#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Compute model and loader timings."""

import pytorch_cls.core.benchmark as benchmark
import pytorch_cls.core.config as config
import pytorch_cls.core.logging as logging
import pytorch_cls.datasets.loader as loader
from pytorch_cls.core.config import cfg


def main():
    config.load_cfg_fom_args("Compute model and loader timings.")
    config.assert_and_infer_cfg()
    train_loader = loader.construct_train_loader()
    benchmark.compute_time_loader(train_loader)


if __name__ == "__main__":
    main()
