# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import abc
from typing import Tuple

import numpy as np
from omegaconf import DictConfig


class DiffDriveVelocityController(abc.ABC):
    """
    Abstract class for differential drive robot velocity controllers.
    """

    @abc.abstractmethod
    def __call__(self, xyt_err: np.ndarray) -> Tuple[float, float, bool]:
        """Contain execution logic, predict velocities for the left and right wheels. Expected to
        return true/false if we have reached this goal and the controller will be moving no
        farther."""
        pass


class DDVelocityControlNoplan(DiffDriveVelocityController):
    """
    Control logic for differential drive robot velocity control.
    Does not plan at all, instead uses heuristics to gravitate towards the goal.
    """

    def __init__(self, cfg: DictConfig):
        self.cfg = cfg

    @staticmethod
    def _velocity_feedback_control(x_err, a, v_max):
        """
        Computes velocity based on distance from target (trapezoidal velocity profile).
        Used for both linear and angular motion.
        """
        t = np.sqrt(2.0 * abs(x_err) / a)  # x_err = (1/2) * a * t^2
        v = min(a * t, v_max)
        return v * np.sign(x_err)

    def _turn_rate_limit(self, lin_err, heading_diff, w_max):
        """
        Compute velocity limit that prevents path from overshooting goal

        heading error decrease rate > linear error decrease rate
        (w - v * np.sin(phi) / D) / phi > v * np.cos(phi) / D
        v < (w / phi) / (np.sin(phi) / D / phi + np.cos(phi) / D)
        v < w * D / (np.sin(phi) + phi * np.cos(phi))

        (D = linear error, phi = angular error)
        """
        assert lin_err >= 0.0
        assert heading_diff >= 0.0

        if heading_diff > self.cfg.max_heading_ang:
            return 0.0
        else:
            return (
                w_max
                * lin_err
                / (np.sin(heading_diff) + heading_diff * np.cos(heading_diff) + 1e-5)
            )

    def __call__(self, xyt_err: np.ndarray) -> Tuple[float, float, bool]:
        v_cmd = w_cmd = 0
        done = True

        # Compute errors
        lin_err_abs = np.linalg.norm(xyt_err[0:2])
        ang_err = xyt_err[2]

        heading_err = np.arctan2(xyt_err[1], xyt_err[0])
        heading_err_abs = abs(heading_err)

        # Go to goal XY position if not there yet
        if lin_err_abs > self.cfg.lin_error_tol:
            # Compute linear velocity -- move towards goal XY
            v_raw = self._velocity_feedback_control(
                lin_err_abs, self.cfg.acc_lin, self.cfg.v_max
            )
            v_limit = self._turn_rate_limit(
                lin_err_abs,
                heading_err_abs,
                self.cfg.w_max / 2.0,
            )
            v_cmd = np.clip(v_raw, 0.0, v_limit)

            # Compute angular velocity -- turn towards goal XY
            w_cmd = self._velocity_feedback_control(
                heading_err, self.cfg.acc_ang, self.cfg.w_max
            )
            done = False

        # Rotate to correct yaw if XY position is at goal
        elif abs(ang_err) > self.cfg.ang_error_tol:
            # Compute angular velocity -- turn to goal orientation
            w_cmd = self._velocity_feedback_control(
                ang_err, self.cfg.acc_ang, self.cfg.w_max
            )
            done = False

        return v_cmd, w_cmd, done
