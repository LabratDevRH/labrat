"""Actor-critic MLP (torch). Kept out of train.py so worker processes never import torch."""
import torch
import torch.nn as nn


class Policy(nn.Module):
    def __init__(self, obs_dim, act_dim, hidden=512):
        super().__init__()
        self.pi = nn.Sequential(nn.Linear(obs_dim, hidden), nn.ELU(), nn.Linear(hidden, hidden), nn.ELU(),
                                nn.Linear(hidden, 256), nn.ELU(), nn.Linear(256, act_dim))
        self.v = nn.Sequential(nn.Linear(obs_dim, hidden), nn.ELU(), nn.Linear(hidden, hidden), nn.ELU(),
                               nn.Linear(hidden, 1))
        self.log_std = nn.Parameter(torch.full((act_dim,), -0.7))
        nn.init.uniform_(self.pi[-1].weight, -1e-3, 1e-3); nn.init.zeros_(self.pi[-1].bias)

    def dist(self, o):
        mu = self.pi(o)
        return torch.distributions.Normal(mu, self.log_std.exp().expand_as(mu))
