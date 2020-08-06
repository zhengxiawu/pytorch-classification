#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Tools for training and testing a model."""

import gc
import os
import random

import numpy as np
import torch
import torch.nn as nn

import pytorch_cls.core.benchmark as benchmark
import pytorch_cls.core.builders as builders
import pytorch_cls.core.checkpoint as checkpoint
import pytorch_cls.core.config as config
import pytorch_cls.core.distributed as dist
import pytorch_cls.core.logging as logging
import pytorch_cls.core.meters as meters
import pytorch_cls.core.net as net
import pytorch_cls.core.optimizer as optim
import pytorch_cls.datasets.loader as loader
from pytorch_cls.core.config import cfg

try:
    from torch.utils.tensorboard import SummaryWriter
except ModuleNotFoundError:
    from tensorboardX import SummaryWriter


logger = logging.get_logger(__name__)
writer = SummaryWriter(log_dir=os.path.join(cfg.OUT_DIR, "tb"))


def setup_env():
    """Sets up environment for training or testing."""
    if dist.is_master_proc():
        # Ensure that the output dir exists
        os.makedirs(cfg.OUT_DIR, exist_ok=True)
        # Save the config
        config.dump_cfg()
    # Setup logging
    logging.setup_logging()
    # Log the config as both human readable and as a json
    logger.info("Config:\n{}".format(cfg))
    logger.info(logging.dump_log_data(cfg, "cfg"))
    # Fix the RNG seeds (see RNG comment in core/config.py for discussion)
    np.random.seed(cfg.RNG_SEED)
    torch.manual_seed(cfg.RNG_SEED)
    torch.cuda.manual_seed_all(cfg.RNG_SEED)
    random.seed(cfg.RNG_SEED)
    # Configure the CUDNN backend
    if cfg.DETERMINSTIC:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.enabled = True
    else:
        torch.backends.cudnn.benchmark = cfg.CUDNN.BENCHMARK


def setup_model():
    """Sets up a model for training or testing and log the results."""
    # Build the model
    model = builders.build_model()
    # logger.info("Model:\n{}".format(model))
    logger.info("Torchversion:{}".format(torch.__version__))
    # Log model complexity
    logger.info(logging.dump_log_data(net.complexity(model), "complexity"))
    # Transfer the model to the current GPU device
    err_str = "Cannot use more GPU devices than available"
    assert cfg.NUM_GPUS <= torch.cuda.device_count(), err_str
    cur_device = torch.cuda.current_device()
    model = model.cuda(device=cur_device)
    # Use multi-process data parallel model in the multi-gpu setting
    if cfg.NUM_GPUS > 1:
        # Make model replica operate on the current device
        model = torch.nn.parallel.DistributedDataParallel(
            module=model, device_ids=[cur_device], output_device=cur_device
        )
        # Set complexity function to be module's complexity function
        model.complexity = model.module.complexity
    return model


def train_epoch(train_loader, model, loss_fun, optimizer, train_meter, cur_epoch):
    """Performs one epoch of training."""
    # Shuffle the data
    loader.shuffle(train_loader, cur_epoch)
    # Update the learning rate
    lr = optim.get_epoch_lr(cur_epoch)
    optim.set_lr(optimizer, lr)
    # Enable training mode
    model.train()
    train_meter.iter_tic()
    # scale the grad in amp, amp only support the newest version
    scaler = torch.cuda.amp.GradScaler() if cfg.TRAIN.AMP & hasattr(
        torch.cuda.amp, 'autocast') else None
    for cur_iter, (inputs, labels) in enumerate(train_loader):
        # Transfer the data to the current GPU device
        inputs, labels = inputs.cuda(), labels.cuda(non_blocking=True)
        # using AMP
        if scaler is not None:
            with torch.cuda.amp.autocast():
                if cfg.DARTS.AUX_WEIGHT > 0 and cfg.MODEL.TYPE == 'darts_cnn':
                    preds, aux_preds = model(inputs)
                    loss = loss_fun(preds, labels)
                    loss += cfg.DARTS.AUX_WEIGHT * loss_fun(aux_preds, labels)
                else:
                    # Perform the forward pass in AMP
                    preds = model(inputs)
                    # Compute the loss in AMP
                    loss = loss_fun(preds, labels)
                # Perform the backward pass in AMP
                optimizer.zero_grad()
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                # Updates the scale for next iteration.
                scaler.update()
        else:
            if cfg.DARTS.AUX_WEIGHT > 0 and cfg.MODEL.TYPE == 'darts_cnn':
                preds, aux_preds = model(inputs)
                loss = loss_fun(preds, labels)
                loss += cfg.DARTS.AUX_WEIGHT * loss_fun(aux_preds, labels)
            else:
                preds = model(inputs)
                # Compute the loss
                loss = loss_fun(preds, labels)
            # Perform the backward pass
            optimizer.zero_grad()
            loss.backward()
            if cfg.OPTIM.GRAD_CLIP > 0:
                nn.utils.clip_grad_norm_(model.parameters(), cfg.OPTIM.GRAD_CLIP)
            # Update the parameters
            optimizer.step()
        # Compute the errors
        top1_err, top5_err = meters.topk_errors(preds, labels, [1, 5])
        # Combine the stats across the GPUs (no reduction if 1 GPU used)
        loss, top1_err, top5_err = dist.scaled_all_reduce(
            [loss, top1_err, top5_err])
        # Copy the stats from GPU to CPU (sync point)
        loss, top1_err, top5_err = loss.item(), top1_err.item(), top5_err.item()
        train_meter.iter_toc()
        # Update and log stats
        mb_size = inputs.size(0) * cfg.NUM_GPUS
        train_meter.update_stats(top1_err, top5_err, loss, lr, mb_size)
        train_meter.log_iter_stats(cur_epoch, cur_iter)
        train_meter.iter_tic()
    # Log epoch stats
    train_stats = train_meter.get_epoch_stats(cur_epoch)
    writer.add_scalar('train/top1', train_stats['top1_err'], cur_epoch)
    writer.add_scalar('train/top5', train_stats['top5_err'], cur_epoch)
    train_meter.log_epoch_stats(cur_epoch)
    train_meter.reset()


@torch.no_grad()
def test_epoch(test_loader, model, test_meter, cur_epoch):
    """Evaluates the model on the test set."""
    # Enable eval mode
    model.eval()
    test_meter.iter_tic()
    for cur_iter, (inputs, labels) in enumerate(test_loader):
        # Transfer the data to the current GPU device
        inputs, labels = inputs.cuda(), labels.cuda(non_blocking=True)
        # using AMP
        if cfg.TEST.AMP & hasattr(torch.cuda.amp, 'autocast'):
            with torch.cuda.amp.autocast():
                # Compute the predictions
                if cfg.DARTS.AUX_WEIGHT > 0 and cfg.MODEL.TYPE == 'darts_cnn':
                    preds, aux_preds = model(inputs)
                else:
                    preds = model(inputs)
        else:
            # Compute the predictions
            if cfg.DARTS.AUX_WEIGHT > 0 and cfg.MODEL.TYPE == 'darts_cnn':
                preds, aux_preds = model(inputs)
            else:
                preds = model(inputs)
        # Compute the errors
        top1_err, top5_err = meters.topk_errors(preds, labels, [1, 5])
        # Combine the errors across the GPUs  (no reduction if 1 GPU used)
        top1_err, top5_err = dist.scaled_all_reduce([top1_err, top5_err])
        # Copy the errors from GPU to CPU (sync point)
        top1_err, top5_err = top1_err.item(), top5_err.item()
        test_meter.iter_toc()
        # Update and log stats
        test_meter.update_stats(
            top1_err, top5_err, inputs.size(0) * cfg.NUM_GPUS)
        test_meter.log_iter_stats(cur_epoch, cur_iter)
        test_meter.iter_tic()
    # Log epoch stats
    test_stats = test_meter.get_epoch_stats(cur_epoch)
    writer.add_scalar('test/top1', test_stats['top1_err'], cur_epoch)
    writer.add_scalar('test/top5', test_stats['top5_err'], cur_epoch)
    test_meter.log_epoch_stats(cur_epoch)
    test_meter.reset()


def train_model():
    """Trains the model."""
    # Setup training/testing environment
    setup_env()
    # Construct the model, loss_fun, and optimizer
    model = setup_model()
    loss_fun = builders.build_loss_fun().cuda()
    optimizer = optim.construct_optimizer(model)
    # Load checkpoint or initial weights
    start_epoch = 0
    if cfg.TRAIN.AUTO_RESUME and checkpoint.has_checkpoint():
        last_checkpoint = checkpoint.get_last_checkpoint()
        checkpoint_epoch = checkpoint.load_checkpoint(
            last_checkpoint, model, optimizer)
        logger.info("Loaded checkpoint from: {}".format(last_checkpoint))
        start_epoch = checkpoint_epoch + 1
    elif cfg.TRAIN.WEIGHTS:
        checkpoint.load_checkpoint(cfg.TRAIN.WEIGHTS, model)
        logger.info("Loaded initial weights from: {}".format(
            cfg.TRAIN.WEIGHTS))
    # Create data loaders and meters
    train_loader = loader.construct_train_loader()
    test_loader = loader.construct_test_loader()
    train_meter = meters.TrainMeter(len(train_loader))
    test_meter = meters.TestMeter(len(test_loader))
    # Compute model and loader timings
    if start_epoch == 0 and cfg.PREC_TIME.NUM_ITER > 0:
        benchmark.compute_time_full(model, loss_fun, train_loader, test_loader)
    # Perform the training loop
    logger.info("Start epoch: {}".format(start_epoch + 1))
    for cur_epoch in range(start_epoch, cfg.OPTIM.MAX_EPOCH):
        if cfg.MODEL.TYPE == 'darts_cnn' and cfg.DARTS.DROP_PATH_PROB > 0:
            drop_prob = cfg.DARTS.DROP_PATH_PROB * cur_epoch / cfg.OPTIM.MAX_EPOCH
            if cfg.NUM_GPUS > 1:
                model.module.drop_path_prob(drop_prob)
            else:
                model.drop_path_prob(drop_prob)
        # Train for one epoch
        train_epoch(train_loader, model, loss_fun,
                    optimizer, train_meter, cur_epoch)
        # Compute precise BN stats
        if cfg.BN.USE_PRECISE_STATS:
            net.compute_precise_bn_stats(model, train_loader)
        # Save a checkpoint
        if (cur_epoch + 1) % cfg.TRAIN.CHECKPOINT_PERIOD == 0:
            checkpoint_file = checkpoint.save_checkpoint(
                model, optimizer, cur_epoch)
            logger.info("Wrote checkpoint to: {}".format(checkpoint_file))
        # Evaluate the model
        next_epoch = cur_epoch + 1
        if next_epoch % cfg.TRAIN.EVAL_PERIOD == 0 or next_epoch == cfg.OPTIM.MAX_EPOCH:
            logger.info("Start testing")
            test_epoch(test_loader, model, test_meter, cur_epoch)
        # save best
        if test_meter.is_best:
            checkpoint_file = checkpoint.save_checkpoint(
                model, optimizer, cur_epoch, test_meter.is_best)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()  # https://forums.fast.ai/t/clearing-gpu-memory-pytorch/14637
        gc.collect()


def test_model():
    """Evaluates a trained model."""
    # Setup training/testing environment
    setup_env()
    # Construct the model
    model = setup_model()
    # Load model weights
    checkpoint.load_checkpoint(cfg.TEST.WEIGHTS, model)
    logger.info("Loaded model weights from: {}".format(cfg.TEST.WEIGHTS))
    # Create data loaders and meters
    test_loader = loader.construct_test_loader()
    test_meter = meters.TestMeter(len(test_loader))
    # Evaluate the model
    test_epoch(test_loader, model, test_meter, 0)


def time_model():
    """Times model and data loader."""
    # Setup training/testing environment
    setup_env()
    # Construct the model and loss_fun
    model = setup_model()
    loss_fun = builders.build_loss_fun().cuda()
    # Create data loaders
    train_loader = loader.construct_train_loader()
    test_loader = loader.construct_test_loader()
    # Compute model and loader timings
    benchmark.compute_time_full(model, loss_fun, train_loader, test_loader)
