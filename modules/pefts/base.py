import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import torch
import torch.nn as nn
import math
import numpy as np
from configs.pefts import Peft_Config, Lora_Config

class Base_Adapter(nn.Module):

    def __init__(
        self,
        base_layer: nn.Module
    ) -> None:
        super().__init__()

        self.base_layer = base_layer
        self.disabled = False

    def set_adapter(self, enable: bool, base_enable: bool = False):

        if enable:
            self.base_layer.requires_grad_(False)
            self.disabled = False
        else:
            self.base_layer.requires_grad_(base_enable)
            self.base_layer.requires_grad_(base_enable)
            self.disabled = True

    def forward(self, x: torch.Tensor, *args, **kwargs):

        return self.base_layer(x, *args, **kwargs)