# Copyright (c) OpenMMLab. All rights reserved.
import math
import warnings

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as cp
from mmcv.cnn import Conv2d, build_activation_layer, build_norm_layer, ConvModule
from mmcv.cnn.bricks.drop import build_dropout
from mmcv.cnn.bricks.transformer import MultiheadAttention
from mmengine.model import BaseModule, ModuleList, Sequential
from mmengine.model.weight_init import (constant_init, normal_init,
                                        trunc_normal_init)

from mmseg.registry import MODELS
from ..utils import PatchEmbed, nchw_to_nlc, nlc_to_nchw
from .utils import set_requires_grad, set_train
from .mit import MixFFN, EfficientMultiheadAttention, MixVisionTransformer, TransformerEncoderLayer


class _LoRA_qkv_EfficientMultiheadAttention(EfficientMultiheadAttention):
    """In nn.MultiheadAttention it is implemented as
    self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)

    B, N, C = x.shape
    qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, self.head_dim).permute(2, 0, 3, 1, 4)
    q, k, v = qkv.unbind(0)

    """

    def __init__(
        self,
        qkv: nn.Module, # EfficientMultiheadAttention
        r: int,
        alpha: int,
        embed_dims,
        num_heads,
        attn_drop=0.,
        proj_drop=0.,
        dropout_layer=None,
        init_cfg=None,
        batch_first=True,
        qkv_bias=False,
        norm_cfg=dict(type='LN'),
        sr_ratio=1
    ):
        super().__init__(
            embed_dims=embed_dims,
            num_heads=num_heads,
            attn_drop=attn_drop,
            proj_drop=proj_drop,
            dropout_layer=dropout_layer,
            batch_first=batch_first,
            qkv_bias=qkv_bias,
            norm_cfg=norm_cfg,
            sr_ratio=sr_ratio
        )
        self.attn = qkv

        self.adapt_linear_a_q = nn.Linear(embed_dims, r, bias=False)
        self.adapt_linear_b_q = nn.Linear(r, embed_dims, bias=False)
        self.adapt_linear_a_v = nn.Linear(embed_dims, r, bias=False)
        self.adapt_linear_b_v = nn.Linear(r, embed_dims, bias=False)

        with torch.no_grad():
            # Default initilization
            nn.init.kaiming_uniform_(self.adapt_linear_a_q.weight, a=math.sqrt(5))
            nn.init.kaiming_uniform_(self.adapt_linear_a_v.weight, a=math.sqrt(5))
            nn.init.zeros_(self.adapt_linear_b_q.weight)
            nn.init.zeros_(self.adapt_linear_b_v.weight)

        self.r = r
        self.alpha = alpha

    def forward(self, x, hw_shape, identity=None):
        x_q = x
        if self.sr_ratio > 1:
            x_kv = nlc_to_nchw(x, hw_shape)
            x_kv = self.sr(x_kv)
            x_kv = nchw_to_nlc(x_kv)
            x_kv = self.norm(x_kv)
        else:
            x_kv = x

        if identity is None:
            identity = x_q

        # Because the dataflow('key', 'query', 'value') of
        # ``torch.nn.MultiheadAttention`` is (num_query, batch,
        # embed_dims), We should adjust the shape of dataflow from
        # batch_first (batch, num_query, embed_dims) to num_query_first
        # (num_query ,batch, embed_dims), and recover ``attn_output``
        # from num_query_first to batch_first.
        if self.batch_first:
            x_q = x_q.transpose(0, 1)
            x_kv = x_kv.transpose(0, 1)
            
        new_q = self.adapt_linear_b_q(self.adapt_linear_a_q(x_q))
        new_v = self.adapt_linear_b_v(self.adapt_linear_a_v(x_kv))

        x_q = x_q + new_q * (self.alpha // self.r)
        x_kv = x_kv + new_v * (self.alpha // self.r)

        # print(x_q.shape, x_kv.shape)
        out = self.attn(query=x_q, key=x_kv, value=x_kv)[0]

        if self.batch_first:
            out = out.transpose(0, 1)

        return identity + self.dropout_layer(self.proj_drop(out))



class TransformerEncoderLayer_lora(BaseModule):
    """Implements one encoder layer in Segformer.

    Args:
        embed_dims (int): The feature dimension.
        num_heads (int): Parallel attention heads.
        feedforward_channels (int): The hidden dimension for FFNs.
        drop_rate (float): Probability of an element to be zeroed.
            after the feed forward layer. Default 0.0.
        attn_drop_rate (float): The drop out rate for attention layer. 
            Default 0.0.
        drop_path_rate (float): stochastic depth rate. Default 0.0.
        qkv_bias (bool): enable bias for qkv if True.
            Default: True.
        act_cfg (dict): The activation config for FFNs.
            Default: dict(type='GELU').
        norm_cfg (dict): Config dict for normalization layer.
            Default: dict(type='LN').
        batch_first (bool): Key, Query and Value are shape of
            (batch, n, embed_dim)
            or (n, batch, embed_dim). Default: False.
        init_cfg (dict, optional): Initialization config dict.
            Default:None.
        sr_ratio (int): The ratio of spatial reduction of Efficient Multi-head
            Attention of Segformer. Default: 1.
        with_cp (bool): Use checkpoint or not. Using checkpoint will save
            some memory while slowing down the training speed. Default: False.
    """

    def __init__(self,
                 embed_dims,
                 num_heads,
                 feedforward_channels,
                 drop_rate=0.,
                 attn_drop_rate=0.,
                 drop_path_rate=0.,
                 qkv_bias=True,
                 act_cfg=dict(type='GELU'),
                 norm_cfg=dict(type='LN'),
                 batch_first=True,
                 sr_ratio=1,
                 with_cp=False):
        super().__init__()
        r = 16
        # The ret[0] of build_norm_layer is norm name.
        self.norm1 = build_norm_layer(norm_cfg, embed_dims)[1]

        self.attn = EfficientMultiheadAttention(
            embed_dims=embed_dims,
            num_heads=num_heads,
            attn_drop=attn_drop_rate,
            proj_drop=drop_rate,
            dropout_layer=dict(type='DropPath', drop_prob=drop_path_rate),
            batch_first=batch_first,
            qkv_bias=qkv_bias,
            norm_cfg=norm_cfg,
            sr_ratio=sr_ratio)

        # The ret[0] of build_norm_layer is norm name.
        self.norm2 = build_norm_layer(norm_cfg, embed_dims)[1]

        self.ffn = MixFFN(
            embed_dims=embed_dims,
            feedforward_channels=feedforward_channels,
            ffn_drop=drop_rate,
            dropout_layer=dict(type='DropPath', drop_prob=drop_path_rate),
            act_cfg=act_cfg)

        self.with_cp = with_cp


        self.attn = _LoRA_qkv_EfficientMultiheadAttention(
            self.attn.attn,       
            r,
            r,
            embed_dims=embed_dims,
            num_heads=num_heads,
            attn_drop=attn_drop_rate,
            proj_drop=drop_rate,
            dropout_layer=dict(type='DropPath', drop_prob=drop_path_rate),
            batch_first=batch_first,
            qkv_bias=qkv_bias,
            norm_cfg=norm_cfg,
            sr_ratio=sr_ratio,
        )
    
    def forward(self, x, hw_shape):
        def _inner_forward(x):
            x = self.attn(self.norm1(x), hw_shape, identity=x)
            x = self.ffn(self.norm2(x), hw_shape, identity=x)
            return x

        if self.with_cp and x.requires_grad:
            x = cp.checkpoint(_inner_forward, x)
        else:
            x = _inner_forward(x)
        return x

@MODELS.register_module()
class MixVisionTransformer_LoRA(MixVisionTransformer):
    def __init__(self, in_channels=3, embed_dims=64, num_stages=4, num_layers=[3, 4, 6, 3], num_heads=[1, 2, 4, 8], patch_sizes=[7, 3, 3, 3], strides=[4, 2, 2, 2], sr_ratios=[8, 4, 2, 1], out_indices=(0, 1, 2, 3), mlp_ratio=4, qkv_bias=True, drop_rate=0, attn_drop_rate=0, drop_path_rate=0, act_cfg=dict(type='GELU'), norm_cfg=dict(type='LN', eps=0.000001), pretrained=None, init_cfg=None, with_cp=False):
        super().__init__(in_channels, embed_dims, num_stages, num_layers, num_heads, patch_sizes, strides, sr_ratios, out_indices, mlp_ratio, qkv_bias, drop_rate, attn_drop_rate, drop_path_rate, act_cfg, norm_cfg, pretrained, init_cfg, with_cp)

        # transformer encoder
        dpr = [
            x.item()
            for x in torch.linspace(0, drop_path_rate, sum(num_layers))
        ]  # stochastic num_layer decay rule

        cur = 0
        self.layers = ModuleList()
        for i, num_layer in enumerate(num_layers):
            embed_dims_i = embed_dims * num_heads[i]
            patch_embed = PatchEmbed(
                in_channels=in_channels,
                embed_dims=embed_dims_i,
                kernel_size=patch_sizes[i],
                stride=strides[i],
                padding=patch_sizes[i] // 2,
                norm_cfg=norm_cfg)
            transformer_layer = TransformerEncoderLayer_lora
            layer = ModuleList([
                transformer_layer(
                    embed_dims=embed_dims_i,
                    num_heads=num_heads[i],
                    feedforward_channels=mlp_ratio * embed_dims_i,
                    drop_rate=drop_rate,
                    attn_drop_rate=attn_drop_rate,
                    drop_path_rate=dpr[cur + idx],
                    qkv_bias=qkv_bias,
                    act_cfg=act_cfg,
                    norm_cfg=norm_cfg,
                    with_cp=with_cp,
                    sr_ratio=sr_ratios[i]
                    ) for idx in range(num_layer)
            ])
            in_channels = embed_dims_i
            # The ret[0] of build_norm_layer is norm name.
            norm = build_norm_layer(norm_cfg, embed_dims_i)[1]
            self.layers.append(ModuleList([patch_embed, layer, norm]))
            cur += num_layer

    def set_cloud(self, mode = True):
        for i, layer in enumerate(self.layers):
            for block in layer[1]:
                block.encode_only = False

    def train(self, mode: bool = True):
        if not mode:
            return super().train(mode)
        set_requires_grad(self, ["adapt"])
        set_train(self, ["adapt"])

    def state_dict(self, destination, prefix, keep_vars):
        state = super().state_dict(destination, prefix, keep_vars)
        keys = [k for k in state.keys() if "adapt" not in k]
        for key in keys:
            state.pop(key)
            if key in destination:
                destination.pop(key)
        return state
    
    def forward(self, x):
        outs = self.forward_encode(x)
        return outs

    def forward_encode(self, x):
        outs = []
        for i, layer in enumerate(self.layers):
            # print(i, layer)
            x, hw_shape = layer[0](x)
            for block in layer[1]:
                x = block(x, hw_shape)
            x = layer[2](x)
            x = nlc_to_nchw(x, hw_shape)
            if i in self.out_indices:
                outs.append(x)
        return outs