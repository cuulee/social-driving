from typing import List, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F
from gym.spaces import Box, Discrete
from gym.spaces import Tuple as GSTuple
from sdriving.agents.utils import mlp
from torch import nn
from torch.distributions.categorical import Categorical
from torch.distributions.normal import Normal

EPS = 1e-7
LOG_STD_MAX = 2
LOG_STD_MIN = -20


# Code Credits -- Jun Gao
# Fit a spline given k control points.
class ActiveSplineTorch(nn.Module):
    def __init__(self, cp_num, p_num, alpha=0.5, device="cpu"):
        super(ActiveSplineTorch, self).__init__()
        self.cp_num = cp_num
        self.p_num = int(p_num / cp_num)
        self.alpha = alpha
        self.device = device

    def batch_arange(self, start_t, end_t, step_t):
        batch_arr = map(torch.arange, start_t, end_t, step_t)
        batch_arr = [arr.unsqueeze(0) for arr in batch_arr]
        return torch.cat(batch_arr, dim=0)

    def batch_linspace(self, start_t, end_t, step_t, device="cuda"):
        step_t = [step_t] * end_t.size(0)
        batch_arr = map(torch.linspace, start_t, end_t, step_t)
        batch_arr = [arr.unsqueeze(0) for arr in batch_arr]
        return torch.cat(batch_arr, dim=0).to(device)

    def forward(self, cps):
        return self.sample_point(cps)

    def sample_point(self, cps):
        cp_num = cps.size(1)
        cps = torch.cat([cps, cps[:, 0, :].unsqueeze(1)], dim=1)
        auxillary_cps = torch.zeros(
            cps.size(0),
            cps.size(1) + 2,
            cps.size(2),
            device=cps.device,
            dtype=torch.float,
        )
        auxillary_cps[:, 1:-1, :] = cps

        l_01 = torch.sqrt(
            torch.sum(torch.pow(cps[:, 0, :] - cps[:, 1, :], 2), dim=1) + EPS
        )
        l_last_01 = torch.sqrt(
            torch.sum(torch.pow(cps[:, -1, :] - cps[:, -2, :], 2), dim=1) + EPS
        )

        l_01.detach_().unsqueeze_(1)
        l_last_01.detach_().unsqueeze_(1)

        auxillary_cps[:, 0, :] = cps[:, 0, :] - l_01 / l_last_01 * (
            cps[:, -1, :] - cps[:, -2, :]
        )
        auxillary_cps[:, -1, :] = cps[:, -1, :] + l_last_01 / l_01 * (
            cps[:, 1, :] - cps[:, 0, :]
        )

        t = torch.zeros(
            [auxillary_cps.size(0), auxillary_cps.size(1)],
            device=cps.device,
            dtype=torch.float,
        )
        for i in range(1, t.size(1)):
            t[:, i] = (
                torch.pow(
                    torch.sqrt(
                        torch.sum(
                            torch.pow(
                                auxillary_cps[:, i, :]
                                - auxillary_cps[:, i - 1, :],
                                2,
                            ),
                            dim=1,
                        )
                    ),
                    self.alpha,
                )
                + t[:, i - 1]
            )

        # No need to calculate gradient w.r.t t.
        t = t.detach()
        lp = 0
        points = torch.zeros(
            [cps.size(0), self.p_num * self.cp_num, cps.size(2)],
            device=cps.device,
            dtype=torch.float,
        )

        for sg in range(1, self.cp_num + 1):
            v = self.batch_linspace(
                t[:, sg], t[:, sg + 1], self.p_num, cps.device
            )
            t0 = t[:, sg - 1].unsqueeze(1)
            t1 = t[:, sg].unsqueeze(1)
            t2 = t[:, sg + 1].unsqueeze(1)
            t3 = t[:, sg + 2].unsqueeze(1)

            for i in range(self.p_num):
                tv = v[:, i].unsqueeze(1)
                x01 = (t1 - tv) / (t1 - t0) * auxillary_cps[:, sg - 1, :] + (
                    tv - t0
                ) / (t1 - t0) * auxillary_cps[:, sg, :]
                x12 = (t2 - tv) / (t2 - t1) * auxillary_cps[:, sg, :] + (
                    tv - t1
                ) / (t2 - t1) * auxillary_cps[:, sg + 1, :]
                x23 = (t3 - tv) / (t3 - t2) * auxillary_cps[:, sg + 1, :] + (
                    tv - t2
                ) / (t3 - t2) * auxillary_cps[:, sg + 2, :]
                x012 = (t2 - tv) / (t2 - t0) * x01 + (tv - t0) / (
                    t2 - t0
                ) * x12
                x123 = (t3 - tv) / (t3 - t1) * x12 + (tv - t1) / (
                    t3 - t1
                ) * x23
                points[:, lp] = (t2 - tv) / (t2 - t1) * x012 + (tv - t1) / (
                    t2 - t1
                ) * x123
                lp = lp + 1

        return points


class PPOActor(nn.Module):
    def sample(self, pi):
        raise NotImplementedError

    def _distribution(self, obs):
        raise NotImplementedError

    def _deterministic(self, obs):
        raise NotImplementedError

    def _log_prob_from_distribution(self, pi, act):
        return pi.log_prob(act)

    def forward(self, obs, act=None):
        pi = self._distribution(obs)
        logp_a = None
        if act is not None:
            logp_a = self._log_prob_from_distribution(pi, act)
        return pi, logp_a


class PPOCategoricalActor(PPOActor):
    def sample(self, pi):
        return pi.sample()

    def _get_logits(self, obs):
        raise NotImplementedError

    def _deterministic(self, obs):
        return torch.argmax(self._get_logits(obs), dim=-1)

    def _distribution(self, obs):
        return Categorical(logits=self._get_logits(obs))


class PPOLidarCategoricalActor(PPOCategoricalActor):
    def __init__(
        self,
        obs_dim: int,
        act_space: Discrete,
        hidden_sizes: Union[List[int], Tuple[int]],
        activation: torch.nn.Module,
        history_len: int,
        feature_dim: int = 25,
    ):
        super().__init__()
        self.logits_net = mlp(
            [obs_dim + feature_dim] + list(hidden_sizes) + [act_space.n],
            activation,
        )
        self.lidar_features = nn.Sequential(
            nn.Conv1d(history_len, 1, 4, 2, 2, padding_mode="circular"),
            nn.Conv1d(1, 1, 4, 2, 2, padding_mode="circular"),
            nn.AdaptiveAvgPool1d(feature_dim),
        )
        self.history_len = history_len

    def _get_logits(self, obs: Union[Tuple[torch.Tensor], List[torch.Tensor]]):
        bsize = obs[0].size(0) if obs[0].ndim > 1 else 1
        features = self.lidar_features(
            obs[1].view(bsize, self.history_len, -1)
        ).view(bsize, -1)
        if obs[1].ndim == 1:
            features = features.view(-1)

        return self.logits_net(torch.cat([obs[0], features], dim=-1))


class PPOGaussianActor(PPOActor):
    def sample(self, pi):
        return self.act_scale(torch.tanh(pi.rsample()))

    def _get_mu_std(self, obs, std):
        raise NotImplementedError

    def _distribution(self, obs):
        return Normal(*self._get_mu_std(obs, True))

    def _deterministic(self, obs):
        return self.act_scale(torch.tanh(self._get_mu_std(obs, False)))

    def _log_prob_from_distribution(self, pi, act):
        act = self.act_rescale(act)
        if act.ndim == 1:
            act = act.unsqueeze(0)
        logp = pi.log_prob(act).sum(axis=-1)
        logp = logp - (2 * (np.log(2) - act - F.softplus(-2 * act))).sum(
            axis=1
        )
        return logp.view(-1)


class PPOLidarGaussianActor(PPOGaussianActor):
    def __init__(
        self,
        obs_dim: int,
        act_space: Box,
        hidden_sizes: Union[List[int], Tuple[int]],
        activation: torch.nn.Module,
        history_len: int,
        feature_dim: int = 25,
    ):
        super().__init__()
        act_dim = act_space.shape[0]
        self.act_high = torch.as_tensor(act_space.high)
        self.act_low = torch.as_tensor(act_space.low)
        self.net = mlp(
            [obs_dim + feature_dim] + list(hidden_sizes),
            activation,
            activation,
        )
        self.mu_layer = nn.Linear(hidden_sizes[-1], act_dim)
        self.lidar_features = nn.Sequential(
            nn.Conv1d(history_len, 1, 4, 2, 2, padding_mode="circular"),
            nn.Conv1d(1, 1, 4, 2, 2, padding_mode="circular"),
            nn.AdaptiveAvgPool1d(feature_dim),
        )
        self.log_std = nn.Parameter(-0.5 * torch.ones(act_dim))
        self.history_len = history_len

    def act_scale(self, act):
        if not act.device == self.act_high.device:
            self.act_high = self.act_high.to(act.device)
            self.act_low = self.act_low.to(act.device)
        return (act + 1) * 0.5 * (self.act_high - self.act_low) + self.act_low

    def act_rescale(self, act):
        return self.atanh(
            2 * (act - self.act_low) / (self.act_high - self.act_low) - 1.0
        )

    @staticmethod
    def atanh(x):
        return 0.5 * torch.log(torch.abs((1 + x + 1e-7) / (1 - x + 1e-7)))

    def _get_mu_std(
        self,
        obs: Union[Tuple[torch.Tensor], List[torch.Tensor]],
        std: bool = True,
    ):
        bsize = obs[0].size(0) if obs[0].ndim > 1 else 1
        features = self.lidar_features(
            obs[1].view(bsize, self.history_len, -1)
        ).view(bsize, -1)
        if obs[1].ndim == 1:
            features = features.view(-1)

        out = self.net(torch.cat([obs[0], features], dim=-1))

        if std:
            return (
                self.mu_layer(out),
                torch.exp(torch.clamp(self.log_std, LOG_STD_MIN, LOG_STD_MAX)),
            )
            return mu, std
        else:
            return self.mu_layer(out)


class PPOLidarCentralizedCritic(nn.Module):
    def __init__(
        self,
        obs_dim: int,
        hidden_sizes: Union[List[int], Tuple[int]],
        activation: torch.nn.Module,
        history_len: int,
        nagents: int,
        feature_dim: int = 25,
    ):
        super().__init__()
        self.v_net = mlp(
            [(obs_dim + feature_dim) * nagents] + list(hidden_sizes) + [1],
            activation,
        )
        self.lidar_features = nn.Sequential(
            nn.Conv1d(history_len, 1, 4, 2, 2, padding_mode="circular"),
            nn.Conv1d(1, 1, 4, 2, 2, padding_mode="circular"),
            nn.AdaptiveAvgPool1d(feature_dim),
        )
        self.history_len = history_len
        self.nagents = nagents

    def forward(
        self, obs_list: List[Union[Tuple[torch.Tensor], List[torch.Tensor]]]
    ):
        assert len(obs_list) == self.nagents

        f_vecs = []
        state_vec = torch.cat([o for o, _ in obs_list], dim=-1)

        for obs in obs_list:
            bsize = obs[1].size(0) if obs[1].ndim > 1 else 1
            features = self.lidar_features(
                obs[1].view(bsize, self.history_len, -1)
            ).view(bsize, -1)
            if obs[1].ndim == 1:
                features = features.view(-1)
            f_vecs.append(features)
        f_vecs = torch.cat(f_vecs, dim=-1)

        return torch.squeeze(
            self.v_net(torch.cat([state_vec, f_vecs], dim=-1)), -1
        )


class PPOLidarPermutationInvariantCentralizedCritic(nn.Module):
    def __init__(
        self,
        obs_dim: int,
        hidden_sizes: Union[List[int], Tuple[int]],
        activation: torch.nn.Module,
        history_len: int,
        feature_dim: int = 25,
    ):
        super().__init__()
        self.feature_net = mlp(
            [obs_dim + feature_dim] + [hidden_sizes[0]],
            activation,
        )
        self.lidar_features = nn.Sequential(
            nn.Conv1d(history_len, 1, 4, 2, 2, padding_mode="circular"),
            nn.Conv1d(1, 1, 4, 2, 2, padding_mode="circular"),
            nn.AdaptiveAvgPool1d(feature_dim),
        )
        self.v_net = mlp(
            list(hidden_sizes) + [1],
            activation,
        )
        self.history_len = history_len

    def forward(
        self, obs_list: List[Union[Tuple[torch.Tensor], List[torch.Tensor]]]
    ):
        f_vecs = []

        for obs in obs_list:
            bsize = obs[1].size(0) if obs[1].ndim > 1 else 1
            features = self.lidar_features(
                obs[1].view(bsize, self.history_len, -1)
            ).view(bsize, -1)
            f_vecs.append(
                self.feature_net(torch.cat([
                    obs[0].view(bsize, -1),
                    features
                ], dim=-1))
            )
        state_vec = sum(f_vecs) / len(f_vecs)

        return torch.squeeze(self.v_net(state_vec), -1)


class PPOLidarDecentralizedCritic(PPOLidarCentralizedCritic):
    def __init__(self, *args, **kwargs):
        if len(args) >= 5:
            args = list(args)
            args[4] = 1
        else:
            kwargs["nagents"] = 1
        super().__init__(*args, **kwargs)

    def forward(self, obs: Union[Tuple[torch.Tensor], List[torch.Tensor]]):
        bsize = obs[1].size(0) if obs[1].ndim > 1 else 1
        features = self.lidar_features(
            obs[1].view(bsize, self.history_len, -1)
        ).view(bsize, -1)
        if obs[1].ndim == 1:
            features = features.view(-1)

        return torch.squeeze(
            self.v_net(torch.cat([obs[0], features], dim=-1)), -1
        )


class PPOLidarActorCritic(nn.Module):
    def __init__(
        self,
        observation_space: GSTuple,
        action_space: Union[Discrete, Box],
        hidden_sizes: Union[List[int], Tuple[int]] = (64, 64),
        activation: torch.nn.Module = nn.Tanh,
        history_len: int = 1,
        feature_dim: int = 25,
        nagents: int = 1,
        centralized: bool = False,
        permutation_invariant: bool = False,
    ):
        super().__init__()

        obs_dim = observation_space[0].shape[0]
        self.centralized = centralized
        self.nagents = nagents

        if isinstance(action_space, Box):
            self.pi = PPOLidarGaussianActor(
                obs_dim,
                action_space,
                hidden_sizes,
                activation,
                history_len,
                feature_dim,
            )
        elif isinstance(action_space, Discrete):
            self.pi = PPOLidarCategoricalActor(
                obs_dim,
                action_space,
                hidden_sizes,
                activation,
                history_len,
                feature_dim,
            )
        else:
            raise Exception(
                "Only Box and Discrete Action Spaces are supported"
            )

        if centralized:
            if permutation_invariant:
                self.v = PPOLidarPermutationInvariantCentralizedCritic(
                    obs_dim,
                    hidden_sizes,
                    activation,
                    history_len,
                    feature_dim
                )
            else:
                self.v = PPOLidarCentralizedCritic(
                    obs_dim,
                    hidden_sizes,
                    activation,
                    history_len,
                    nagents,
                    feature_dim,
                )
        else:
            if permutation_invariant:
                raise Exception(
                    "Permutation Invariance for Decentralized Training not available"
                )
            self.v = PPOLidarDecentralizedCritic(
                obs_dim, hidden_sizes, activation, history_len, feature_dim
            )

    def _step_centralized(self, obs):
        actions = []
        log_probs = []
        with torch.no_grad():
            for o in obs:
                pi = self.pi._distribution(o)
                a = self.pi.sample(pi)
                logp_a = self.pi._log_prob_from_distribution(pi, a)

                actions.append(a)
                log_probs.append(logp_a)
            v = self.v(obs)
        return actions, v, log_probs

    def _step_decentralized(self, obs):
        with torch.no_grad():
            pi = self.pi._distribution(obs)
            a = pi.sample()
            logp_a = self.pi._log_prob_from_distribution(pi, a)

            v = self.v(obs)
            return a, v, logp_a

    def step(self, obs: Union[Tuple[torch.Tensor], List[torch.Tensor]]):
        if self.centralized:
            return self._step_centralized(obs)
        else:
            return self._step_decentralized(obs)

    def act(
        self,
        obs: Union[Tuple[torch.Tensor], List[torch.Tensor]],
        deterministic: bool = True,
    ):
        if deterministic:
            return self.pi._deterministic(obs)
        return self.pi.sample(self.pi._distribution(obs))


class IterativeWayPointPredictor(nn.Module):
    def __init__(
        self,
        hdim: int,
        max_length: int,
        max_width: int,
        separate_goal_model: bool = False,
    ):
        super().__init__()
        obs_dim = 6  # Current LOC, Target LOC, Length, Width
        self.wpoint_disp = nn.Sequential(
            nn.Linear(obs_dim, hdim), nn.ReLU(), nn.Linear(hdim, 2), nn.Tanh()
        )
        if separate_goal_model:
            self.goal_disp = nn.Sequential(
                nn.Linear(obs_dim, hdim),
                nn.ReLU(),
                nn.Linear(hdim, 2),
                nn.Tanh(),
            )
        self.max_length = float(max_length)
        self.max_width = float(max_width)

    def forward(
        self,
        start_pos: torch.Tensor,
        goals: torch.Tensor,
        length: float,
        width: float,
    ):
        # start_pos --> B x 2
        # goals --> B x N x 2
        waypoints = [start_pos.unsqueeze(1)]
        normalize = torch.ones((1, 2)) * (length + width / 2)
        for i in range(goals.size(1)):
            spos = waypoints[-1][:, 0, :]
            gpos = goals[:, i, :]
            obs = torch.cat(
                [
                    spos / normalize,
                    gpos / normalize,
                    torch.ones((spos.size(0), 1)) * length / self.max_length,
                    torch.ones((spos.size(0), 1)) * width / self.max_width,
                ],
                dim=1,
            )  # B x 6
            if hasattr(self, "goal_disp"):
                model = (
                    self.goal_disp
                    if i == goals.size(0) - 1
                    else self.wpoint_disp
                )
            else:
                model = self.wpoint_disp
            point = torch.reshape(model(obs) * width / 2, (-1, 2)) + gpos
            waypoints.append(point.unsqueeze(1))
        return torch.cat(waypoints, dim=1)  # B x (N + 1) x 2
