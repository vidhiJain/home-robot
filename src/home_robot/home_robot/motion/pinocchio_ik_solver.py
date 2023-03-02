# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
from typing import Callable, List, Tuple, Optional, Dict, Union

import numpy as np
import pinocchio
from scipy.spatial.transform import Rotation as R

from home_robot.utils.bullet import PybulletIKSolver

# --DEFAULTS--
# Error tolerances
POS_ERROR_TOL = 0.005
ORI_ERROR_TOL = [0.1, 0.1, np.pi / 2]

# CEM
CEM_MAX_ITERATIONS = 5
CEM_NUM_SAMPLES = 50
CEM_NUM_TOP = 10


class PinocchioIKSolver:
    """IK solver using pinocchio which can handle end-effector constraints for optimized IK solutions"""

    EPS = 1e-4
    DT = 1e-1
    DAMP = 1e-12

    def __init__(self, urdf_path: str, ee_link_name: str, controlled_joints: List[str]):
        """
        urdf_path: path to urdf file
        ee_link_name: name of the end-effector link
        controlled_joints: list of joint names to control
        """
        self.model = pinocchio.buildModelFromUrdf(urdf_path)
        self.data = self.model.createData()
        self.q_neutral = pinocchio.neutral(self.model)

        self.ee_frame_idx = [f.name for f in self.model.frames].index(ee_link_name)
        self.controlled_joints = [
            self.model.idx_qs[self.model.getJointId(j)] if j != "ignore" else -1
            for j in controlled_joints
        ]

    def get_dof(self) -> int:
        """returns dof for the manipulation chain"""
        return len(self.controlled_joints)

    def get_num_controllable_joints(self) -> int:
        """returns number of controllable joints under this solver's purview"""
        return len(self.controlled_joints)

    def _qmap_control2model(self, q_input: np.ndarray) -> np.ndarray:
        """returns a full joint configuration from a partial joint configuration"""
        q_out = self.q_neutral.copy()
        for i, joint_idx in enumerate(self.controlled_joints):
            q_out[joint_idx] = q_input[i]

        return q_out

    def _qmap_model2control(self, q_input: np.ndarray) -> np.ndarray:
        """returns a partial joint configuration from a full joint configuration"""
        q_out = np.empty(len(self.controlled_joints))
        for i, joint_idx in enumerate(self.controlled_joints):
            if joint_idx >= 0:
                q_out[i] = q_input[joint_idx]

        return q_out

    def compute_fk(self, q) -> Tuple[np.ndarray, np.ndarray]:
        """given joint values return end-effector position and quaternion associated with it"""
        q_model = self._qmap_control2model(q)
        pinocchio.forwardKinematics(self.model, self.data, q_model)
        pinocchio.updateFramePlacement(self.model, self.data, self.ee_frame_idx)
        pos = self.data.oMf[self.ee_frame_idx].translation
        quat = R.from_matrix(self.data.oMf[self.ee_frame_idx].rotation).as_quat()

        return pos.copy(), quat.copy()

    def compute_ik(
        self, pos: np.ndarray, quat: np.ndarray, q_init=None, max_iterations=100
    ) -> Tuple[np.ndarray, bool]:
        """given end-effector position and quaternion, return joint values"""
        i = 0
        if q_init is None:
            q = self.q_neutral.copy()
        else:
            q = self._qmap_control2model(q_init)
        desired_ee_pose = pinocchio.SE3(R.from_quat(quat).as_matrix(), pos)
        while True:
            pinocchio.forwardKinematics(self.model, self.data, q)
            pinocchio.updateFramePlacement(self.model, self.data, self.ee_frame_idx)

            dMi = desired_ee_pose.actInv(self.data.oMf[self.ee_frame_idx])
            err = pinocchio.log(dMi).vector
            if np.linalg.norm(err) < self.EPS:
                success = True
                break
            if i >= max_iterations:
                success = False
                break
            J = pinocchio.computeFrameJacobian(
                self.model,
                self.data,
                q,
                self.ee_frame_idx,
                pinocchio.ReferenceFrame.LOCAL,
            )
            v = -J.T.dot(np.linalg.solve(J.dot(J.T) + self.DAMP * np.eye(6), err))
            q = pinocchio.integrate(self.model, q, v * self.DT)
            i += 1

        q_control = self._qmap_model2control(q.flatten())

        return q_control, success


class PositionIKOptimizer:
    """
    Solver that jointly optimizes IK and best orientation to achieve desired position
    """

    max_iterations: int = 30  # Max num of iterations for CEM
    num_samples: int = 100  # Total candidate samples for each CEM iteration
    num_top: int = 10  # Top N candidates for each CEM iteration

    def __init__(
        self,
        ik_solver: Union[PinocchioIKSolver, PybulletIKSolver],
        pos_error_tol: float,
        ori_error_range: Union[float, np.ndarray],
        pos_weight: float = 1.0,
        ori_weight: float = 0.0,
        cem_params: Optional[Dict] = None,
    ):
        self.pos_wt = pos_weight
        self.ori_wt = ori_weight

        # Initialize IK solver
        self.ik_solver = ik_solver

        # Initialize optimizer
        self.pos_error_tol = pos_error_tol
        if type(ori_error_range) is float:
            self.ori_error_range = ori_error_range * np.ones(3)
        else:
            self.ori_error_range = ori_error_range

        cem_params = {} if cem_params is None else cem_params
        max_iterations = (
            cem_params["max_iterations"]
            if "max_iterations" in cem_params
            else self.max_iterations
        )
        num_samples = (
            cem_params["num_samples"]
            if "num_samples" in cem_params
            else self.num_samples
        )
        num_top = cem_params["num_top"] if "num_top" in cem_params else self.num_top

        self.opt = CEM(
            max_iterations=max_iterations,
            num_samples=num_samples,
            num_top=num_top,
            tol=self.pos_error_tol,
            sigma0=self.ori_error_range / 2,
        )

    def compute_ik_opt(self, pose_query: Tuple[np.ndarray, np.ndarray]):
        """optimization-based IK solver using CEM"""
        pos_desired, quat_desired = pose_query

        # Function to optimize: IK error given delta from original desired orientation
        def solve_ik(dr):
            pos = pos_desired
            quat = (R.from_rotvec(dr) * R.from_quat(quat_desired)).as_quat()

            q, _ = self.ik_solver.compute_ik(pos, quat)
            pos_out, rot_out = self.ik_solver.compute_fk(q)

            cost_pos = np.linalg.norm(pos - pos_out)
            cost_rot = (
                1 - (rot_out * quat_desired).sum() ** 2
            )  # TODO: just minimize dr?

            cost = self.pos_wt * cost_pos + self.ori_wt * cost_rot

            return cost, q

        # Optimize for IK and best orientation (x=0 -> use original desired orientation)
        cost_opt, q_result, max_iter, opt_sigma = self.opt.optimize(
            solve_ik, x0=np.zeros(3)
        )
        pos_out, quat_out = self.ik_solver.compute_fk(q_result)
        print(
            f"After ik optimization, cost: {cost_opt}, result: {pos_out, quat_out} vs desired: {pose_query}"
        )
        return q_result, cost_opt, max_iter, opt_sigma


class CEM:
    """class implementing generic CEM solver for optimization"""

    def __init__(
        self,
        max_iterations: int,
        num_samples: int,
        num_top: int,
        tol: float,
        sigma0: np.ndarray,
    ):
        """
        max_iterations: max number of iterations
        num_samples: number of samples per iteration
        num_top: number of top samples to use for next iteration
        tol: tolerance for stopping criterion
        """
        self.max_iterations = max_iterations
        self.num_samples = num_samples
        self.num_top = num_top
        self.cost_tol = tol
        self.sigma0 = sigma0

    def optimize(self, func: Callable, x0: np.ndarray):
        """optimize function func with initial guess mu=x0 and initial std=sigma0"""
        assert (
            x0.shape == self.sigma0.shape
        ), f"x0 and sigma0 must have same shape, got {x0.shape} and {self.sigma0.shape}"

        i = 0
        mu = x0
        sigma = self.sigma0
        while True:
            # Sample x
            x_arr = mu + sigma * np.random.randn(self.num_samples, x0.shape[0])

            # Compute costs
            cost_arr = np.zeros(self.num_samples)
            aux_outputs = [None for _ in range(self.num_samples)]
            for j, x in enumerate(x_arr):
                cost_arr[j], aux_outputs[j] = func(x)

            # Sort costs
            idx_sorted_arr = np.argsort(cost_arr)
            i_best = idx_sorted_arr[0]

            # Check termination
            i += 1
            if (
                i >= self.max_iterations
                or cost_arr[i_best] <= self.cost_tol
                or np.all(sigma <= self.cost_tol / 10)
            ):  # TODO: sigma thresh?
                break

            # Update distribution
            mu = np.mean(x_arr[idx_sorted_arr[: self.num_top], :], axis=0)
            sigma = np.std(x_arr[idx_sorted_arr[: self.num_top], :], axis=0)

        return cost_arr[i_best], aux_outputs[i_best], i, sigma