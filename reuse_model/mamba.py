"""Inference-only Mamba-1, parameter names compatible with the NVIDIA checkpoint.

The selective state update uses float32, without a Linux mamba_ssm extension.
"""
import math
import torch
from torch import nn
from torch.nn import functional as F


def reference_scan(u, delta, a, b, c, skip, z):
    state = u.new_zeros((u.shape[0], u.shape[2], a.shape[1]))
    output = []
    for i in range(u.shape[1]):
        dt = delta[:, i, :, None]
        state = torch.exp(dt * a) * state + dt * u[:, i, :, None] * b[:, i, None, :]
        output.append((state * c[:, i, None, :]).sum(-1) + skip * u[:, i])
    return torch.stack(output, dim=1) * F.silu(z)


class Mamba(nn.Module):
    def __init__(self, d_model, d_state=16, d_conv=4, expand=4):
        super().__init__()
        self.d_inner = int(expand * d_model)
        self.dt_rank = math.ceil(d_model / 16)
        self.d_state = d_state
        self.in_proj = nn.Linear(d_model, self.d_inner * 2, bias=False)
        self.conv1d = nn.Conv1d(self.d_inner, self.d_inner, d_conv, groups=self.d_inner, padding=d_conv-1)
        self.x_proj = nn.Linear(self.d_inner, self.dt_rank + d_state*2, bias=False)
        self.dt_proj = nn.Linear(self.dt_rank, self.d_inner)
        self.A_log = nn.Parameter(torch.zeros(self.d_inner, d_state))
        self.D = nn.Parameter(torch.ones(self.d_inner))
        self.out_proj = nn.Linear(self.d_inner, d_model, bias=False)

    def forward(self, hidden):
        x, z = self.in_proj(hidden).chunk(2, dim=-1)
        x = F.silu(self.conv1d(x.transpose(1, 2))[:, :, :hidden.shape[1]]).transpose(1, 2).contiguous()
        dt, b, c = self.x_proj(x).split([self.dt_rank, self.d_state, self.d_state], dim=-1)
        delta = F.softplus(self.dt_proj(dt)).contiguous()
        a = -torch.exp(self.A_log.float())
        if x.is_cuda:
            from .scan_cuda import selective_scan
            y = selective_scan(x, delta, a, b.contiguous(), c.contiguous(), self.D, z.contiguous())
        else:
            y = reference_scan(x, delta, a, b, c, self.D, z)
        return self.out_proj(y)
