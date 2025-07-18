#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
"""
@File    :   arm_controller.py
@Time    :   2025/06/26 19:20:00
@Author  :   StarCheng
@Version :   1.0
@Site    :   https://star-cheng.github.io/Blog/
"""
from bodyctrl_msgs.msg import CmdSetMotorPosition, SetMotorPosition, MotorStatusMsg, MotorStatus
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from geometry_msgs.msg import PoseStamped, PoseArray, Twist
from controllers.dual_arm_solver import ArmKinematics
from moveit.moveit_solver import MoveItSolver
# from controllers.arms_solver import RobotIKSolver
from std_srvs.srv import Trigger, TriggerResponse
from scipy.spatial.transform import Rotation as R
from sensor_msgs.msg import JointState
from std_msgs.msg import Header
from typing import List
import numpy as np
import threading
import rospy
import math
import time


class ArmController:
    def __init__(self):
        # 左右臂运动学求解器
        # self.arm_left_kinematics = ArmKinematics(True)
        # self.arm_right_kinematics = ArmKinematics(False)
        self.arm_left_kinematics = MoveItSolver(True)
        self.arm_right_kinematics = MoveItSolver(False)
        self.arm_kinematics = [self.arm_right_kinematics, self.arm_left_kinematics]
        self.mirror_ls = [1, -1, -1, 1, -1, 1, -1]

        # 控制器参数
        self.joint_speed = rospy.get_param("~joint_speed", 1)  # rpm
        self.joint_current = rospy.get_param("~joint_current", 5.0)  # A
        self.joint_tolerance = rospy.get_param("~joint_tolerance", 0.01)  # rad
        self.tr_distance = rospy.get_param("~tr_distance", 0.02)  # m
        self.tr_point_time = rospy.get_param("~tr_point_time", min(0.2, 2 * math.pi / self.joint_speed))  # s
        # 线程同步工具
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.target_dict = {}  # 存储当前目标位置 {joint_name: target_position}
        # 关节状态变量
        self.left_joint_status = {"name": [], "pos": [], "speed": [], "current": [], "temp": [], "error": []}
        self.left_joint_positions = [0.0, 0.15, 0.0, -0.0, 0.0, 0.0, 0.0]
        self.right_joint_positions = [0.0, -0.15, 0.0, -0.0, 0.0, 0.0, 0.0]
        self.dual_joint_positions = [self.right_joint_positions, self.left_joint_positions]
        self.left_end_effector_pose, self.right_end_effector_rota, self.left_end_effector_quat = self.arm_left_kinematics.forward_kinematics(
            self.left_joint_positions
        )
        self.right_end_effector_pose, self.right_end_effector_rota, self.right_end_effector_quat = self.arm_right_kinematics.forward_kinematics(
            self.right_joint_positions
        )
        self.joint_names = {True: [i for i in range(11, 18)], False: [j for j in range(21, 28)]}

        # 轨迹规划参数
        self.left_start_pose = None
        self.left_target_pose = None
        self.right_start_pose = None
        self.right_target_pose = None
        self.use_coordinated_motion = False

        # # 设置订阅者
        rospy.Subscriber("/arm/status", MotorStatusMsg, self.arm_status_callback)
        # rospy.Subscriber('/joint_states', JointState, self.joint_states_callback)
        # rospy.Subscriber('/right_arm/target_pose', PoseStamped, self.right_target_callback)
        # rospy.Subscriber('/dual_arm/target_poses', PoseArray, self.coordinated_target_callback)

        # 设置发布者
        self.left_tra_pub = rospy.Publisher("/left_arm/joint_trajectory", JointTrajectory, queue_size=1)
        self.right_tra_pub = rospy.Publisher("/right_arm/joint_trajectory", JointTrajectory, queue_size=1)
        self.arm_cmd_pos_pub = rospy.Publisher("/arm/cmd_pos", CmdSetMotorPosition, queue_size=10)

        # 初始化关节状态
        # self.init_arm_status()
        rospy.sleep(0.1)

    def ik_dual(self, left_target_position, left_target_quaternion, left_initial_angles, right_target_position, right_target_quaternion, right_initial_angles):
        # 优先求解误差小的臂
        left_joints = self.arm_left_kinematics.inverse_kinematics(left_target_position, left_target_quaternion, left_initial_angles)
        right_joints = self.arm_right_kinematics.inverse_kinematics(right_target_position, right_target_quaternion, right_initial_angles)
        # 正解算验证误差
        left_pos, _, left_quat = self.arm_left_kinematics.forward_kinematics(left_joints)
        right_pos, _, right_quat = self.arm_right_kinematics.forward_kinematics(right_joints)
        left_diff = np.linalg.norm(left_pos - left_target_position)
        right_diff = np.linalg.norm(right_pos - right_target_position)
        if left_diff < right_diff:
            for i in range(len(right_joints)):
                left_joints[i]= right_joints[i] * self.mirror_ls[i]
        else:
            for i in range(len(left_joints)):
                right_joints[i]= left_joints[i] * self.mirror_ls[i]
        return left_joints, right_joints

    def arm_status_callback(self, arm_status_msg: MotorStatusMsg):
        name_ls_, pos_ls_, speed_ls_, current_ls_, temperature_ls_, error_ls_, dual_joint_positions_ls_ = [], [], [], [], [], [], [0.0] * 14
        for arm_idx_ in range(14):
            name_ls_.append(arm_status_msg.status[arm_idx_].name)
            pos_ls_.append(arm_status_msg.status[arm_idx_].pos)
            speed_ls_.append(arm_status_msg.status[arm_idx_].speed)
            current_ls_.append(arm_status_msg.status[arm_idx_].current)
            temperature_ls_.append(arm_status_msg.status[arm_idx_].temperature)
            error_ls_.append(arm_status_msg.status[arm_idx_].error)
            dual_joint_positions_ls_[arm_idx_] = arm_status_msg.status[arm_idx_].pos
        self.left_joint_positions = dual_joint_positions_ls_[0:7]
        self.right_joint_positions = dual_joint_positions_ls_[7:14]
        self.left_end_effector_pose, self.left_end_effector_rota, self.left_end_effector_quat = self.arm_left_kinematics.forward_kinematics(
            self.left_joint_positions
        )
        self.right_end_effector_pose, self.right_end_effector_rota, self.right_end_effector_quat = self.arm_right_kinematics.forward_kinematics(
            self.right_joint_positions
        )
        self.dual_joint_positions = [self.right_joint_positions, self.left_joint_positions]
        self.left_joint_status["name"] = name_ls_
        self.left_joint_status["pos"] = pos_ls_
        self.left_joint_status["speed"] = speed_ls_
        self.left_joint_status["current"] = current_ls_
        self.left_joint_status["temp"] = temperature_ls_
        self.left_joint_status["error"] = error_ls_

    def send_arm_cmd_pos(self, name_: int, pos_: float, spd_: float, cur_: float):
        """发送单个关节的目标位置命令
        :param name_: 关节名称 (int)
        :param pos_: 目标位置 (float, 弧度)
        :param spd_: 目标速度 (float, RPM)
        :param cur_: 目标电流 (float, 安培)
        """
        # 创建命令消息
        cmd_msg_ = CmdSetMotorPosition()
        cmd_msg_.header = Header(stamp=rospy.Time.now(), frame_id="")
        # 左臂：11---17, 右臂：21---27
        cmd1 = SetMotorPosition(name=name_, pos=pos_, spd=spd_, cur=cur_)  # 名称  # 弧度  # RPM  # 安培
        cmd_msg_.cmds.append(cmd1)
        # 发布消息
        self.arm_cmd_pos_pub.publish(cmd_msg_)

    def send_arms_cmd_pos(self, name_ls: List[int], pos_ls: List[float], spd_ls: List[float], cur_ls: List[float]):
        """发送多个关节的目标位置命令
        :param name_ls: 关节名称 (list)
        :param pos_ls: 目标位置 (list, 弧度)
        :param spd_ls: 目标速度 (list, RPM)
        :param cur_ls: 目标电流 (list, 安培)
        """
        if len(name_ls) != len(pos_ls) or len(name_ls) != len(spd_ls) or len(name_ls) != len(cur_ls):
            rospy.logerr("The length of name_ls, pos_ls, spd_ls, cur_ls must be equal.")
            return
        # 创建命令消息
        cmd_msgs_ = CmdSetMotorPosition()
        cmd_msgs_.header = Header(stamp=rospy.Time.now(), frame_id="")
        # 左臂：11---17, 右臂：21---27
        for i in range(len(name_ls)):
            cmd = SetMotorPosition(name=name_ls[i], pos=pos_ls[i], spd=spd_ls[i], cur=cur_ls[i])  # 名称 # 弧度  # RPM  # 安培
            cmd_msgs_.cmds.append(cmd)
        self.arm_cmd_pos_pub.publish(cmd_msgs_)

    def send_arms_cmd_pos_service(self, name_ls: List[int], pos_ls: List[float], 
                                 spd_ls: List[float], cur_ls: List[float]) -> bool:
        """
        服务版本的多关节控制命令
        - 发送命令后等待关节到达目标位置(或超时3秒)
        - 返回是否成功到达
        """
        # 发送命令
        self.send_arms_cmd_pos(name_ls, pos_ls, spd_ls, cur_ls)
        
        # 设置目标字典
        with self.lock:
            self.target_dict = dict(zip(name_ls, pos_ls))
        
        # 等待结果或超时
        start_time = time.time()
        timeout = 3.0  # 3.0秒超时
        success = False
        
        with self.condition:
            while not success and (time.time() - start_time) < timeout:
                # 检查是否所有关节都到达目标
                all_reached = True
                for name, target_pos in self.target_dict.items():
                    if name in self.left_joint_status["name"]:
                        idx = self.left_joint_status["name"].index(name)
                        current_pos = self.left_joint_status["pos"][idx]
                        if abs(current_pos - target_pos) > self.joint_tolerance:
                            all_reached = False
                            break
                    else:
                        all_reached = False
                        break
                
                if all_reached:
                    success = True
                    break
                
                # 等待状态更新通知
                remaining_time = timeout - (time.time() - start_time)
                if remaining_time > 0:
                    self.condition.wait(remaining_time)
        
        # 清除目标字典
        with self.lock:
            self.target_dict = {}
        
        return success

    def rotate_single_joint(self, is_left: bool, joint_name: int, rotate_angle: float):
        """旋转关节
        :param is_left: 是否为左臂 (bool)
        :param joint_name: 关节名称 (int), 这里填0-6即可, 分别对于7个关节
        :param joint_angles: 关节角度 (list, 弧度)
        """
        self.send_arm_cmd_pos(self.joint_names[is_left][joint_name], self.dual_joint_positions[is_left][joint_name] + rotate_angle, self.joint_speed, self.joint_current)
        rotate_error = abs(self.dual_joint_positions[is_left][joint_name] + rotate_angle - self.dual_joint_positions[is_left][joint_name])
        rotate_status = rotate_error < self.joint_tolerance
        if rotate_status:
            rospy.loginfo("Rotate Joint %s %s: %s" % (self.joint_names[is_left][joint_name], rotate_angle, rotate_status))
        else:
            rospy.logerr("Rotate Joint %s %s: %s" % (self.joint_names[is_left][joint_name], rotate_angle, rotate_status))
        return rotate_status
    
    def rotate_joint(self, left_rotate_angle, right_rotate_angle):
        if left_rotate_angle is None or right_rotate_angle is None:
            return False
        if len(left_rotate_angle) != len(right_rotate_angle) != 7:
            rospy.logerr("The length of left_rotate_angle and right_rotate_angle must be equal.")
            return False
        # 旋转各个关节轴
        left_joints = self.left_joint_positions
        left_joints = [left_joints[i] + left_rotate_angle[i] for i in range(7)]
        right_joints = self.right_joint_positions
        right_joints = [right_joints[i] + right_rotate_angle[i] for i in range(7)]
        self.send_arms_cmd_pos(self.joint_names[True] + self.joint_names[False], left_joints + right_joints, [self.joint_speed] * 14, [self.joint_current] * 14)
        rospy.sleep(3)
        left_true_error_ = np.linalg.norm(np.array(left_joints) - np.array(self.dual_joint_positions[True]))  # 左臂实际末端位置误差
        right_true_error_ = np.linalg.norm(np.array(right_joints) - np.array(self.dual_joint_positions[False]))  # 右臂实际末端位置误差
        left_arm_move_status, right_arm_move_status = left_true_error_ < self.joint_tolerance, right_true_error_ < self.joint_tolerance
        rospy.loginfo("Dual Arm Rotate Success") if left_arm_move_status and right_arm_move_status else rospy.logerr("Dual Arm Rotate Failed")
        return left_arm_move_status and right_arm_move_status
    
    
    def rotate_arm_joint(self, is_left, arm_joints):
        self.send_arms_cmd_pos(self.joint_names[is_left], arm_joints, [self.joint_speed] * 7, [self.joint_current] * 7)
        rospy.sleep(3)
        arm_true_error_ = np.linalg.norm(np.array(arm_joints) - np.array(self.dual_joint_positions[True]))  # 左臂实际末端位置误差
        arm_move_status = arm_true_error_ < self.joint_tolerance
        rospy.loginfo("Single Arm Rotate Success") if arm_move_status else rospy.logerr("Single Arm Rotate Failed")
        return arm_move_status

    def rotate_dual_joint(self, left_joints, right_joints):
        # 移动各个关节轴
        self.send_arms_cmd_pos_service(self.joint_names[True] + self.joint_names[False], left_joints + right_joints, [self.joint_speed] * 14, [self.joint_current] * 14)
        # rospy.sleep(3)
        left_true_error_ = np.linalg.norm(np.array(left_joints) - np.array(self.dual_joint_positions[True]))  # 左臂实际末端位置误差
        right_true_error_ = np.linalg.norm(np.array(right_joints) - np.array(self.dual_joint_positions[False]))  # 右臂实际末端位置误差
        left_arm_move_status, right_arm_move_status = left_true_error_ < self.joint_tolerance, right_true_error_ < self.joint_tolerance
        rospy.loginfo("Dual Arm Rotate Success") if left_arm_move_status and right_arm_move_status else rospy.logerr("Dual Arm Rotate Failed")
        return left_arm_move_status and right_arm_move_status

    def set_end_effector_target(self, is_left: bool, target_pos: np.ndarray, target_ori: np.ndarray, use_matrix=False):
        # 生成轨迹(2厘米生成一个点, 减少数量以加快计算)
        start_pos, start_rot, start_quat = self.arm_kinematics[is_left].forward_kinematics(self.dual_joint_positions[is_left])
        positions = self.arm_kinematics[is_left].generate_trajectory_by_dist(start_pos, target_pos, self.tr_distance)
        # orientations = self.arm_kinematics[is_left].generate_trajectory_by_dist(start_quat, target_ori, self.tr_distance)
        if len(positions) == 0:
            rospy.logwarn("No trajectory points generated, check target position and distance.")
            positions = [target_pos]  # 如果没有生成点，则直接使用目标位置
        joint_trajectory = []
        all_positions = []
        # 跟踪前一步关节角度
        prev_joints = self.dual_joint_positions[is_left]
        for i, pos in enumerate(positions):
            # print(f"Position: {np.array_str(pos, precision=4, suppress_small=True)}")
            # print(f"Quaternion: {np.array_str(quat, precision=4, suppress_small=True)}")
            # 使用上一步的解作为初始值
            joint_angles_ = self.arm_kinematics[is_left].inverse_kinematics(
                pos,
                target_quaternion=target_ori,
                initial_angles=prev_joints,
                orientation_weight=1.0,
                use_rotation_matrix=use_matrix,
            )
            joint_trajectory.append(joint_angles_)
            # 正向运动学验证
            calc_pos_, calc_rot, calc_quat_ = self.arm_kinematics[is_left].forward_kinematics(joint_angles_)
            all_positions.append(calc_pos_)
            # print(f"FK Position xyz: {np.array_str(calc_pos_, precision=4, suppress_small=True)}")
            # print(f"FK Quaternion wxyz: {np.array_str(calc_quat_, precision=4, suppress_small=True)}")
            # print("Joint Angles (rad):", np.array(joint_angles_).round(2))
            # print("Joint Angles (deg):", np.rad2deg(joint_angles_).round(2))
        return joint_trajectory, all_positions

    def move_along_direction(self, position, quaternion, direction_vector, distance=0.05):
        """
        沿着指定方向（在当前朝向基础上）移动一定距离。
        :param position: 当前位置, 长度为3的数组或列表
        :param quaternion: 当前朝向的四元数(w, x, y, z)
        :param direction_vector: 方向向量（局部坐标系中的方向，例如 [1,0,0] 表示正X方向)
        :param distance: 移动距离, 默认为0.1米
        :return: 新的位姿位置(numpy数组)
        """
        # 转为numpy数组
        position = np.array(position)
        quaternion = np.array(quaternion)
        direction_vector = np.array(direction_vector)
        
        # 转换为scipy支持的四元数顺序(x, y, z, w)
        quat_xyzw = [quaternion[1], quaternion[2], quaternion[3], quaternion[0]]
        
        # 生成旋转对象
        rotation = R.from_quat(quat_xyzw)
        
        # 转换局部方向向量到全局
        global_direction = rotation.apply(direction_vector)
        
        # 计算偏移
        displacement = distance * global_direction
        
        # 计算新位置
        new_position = position + displacement
        
        return new_position

    def move_single_arm_tr(self, is_left, target_pos, target_quat):
        trajectory_, all_points_ = self.set_end_effector_target(is_left, target_pos, target_quat)
        # 创建整个轨迹消息
        for j, (tr_, pos_) in enumerate(zip(trajectory_, all_points_)):
            speeds = [self.joint_speed] * 7  # RPM值
            currents = [self.joint_current] * 7  # 电流值(安培)
            self.send_arms_cmd_pos_service(self.joint_names[is_left], tr_, speeds, currents)

    def move_single_arm_by_xyz(self, is_left: bool, target_pos: np.ndarray, target_quat: np.ndarray):
        left_target_joint_ = self.arm_kinematics[is_left].inverse_kinematics(target_pos, target_quat, self.dual_joint_positions[is_left])
        self.send_arms_cmd_pos(self.joint_names[is_left], left_target_joint_, [self.joint_speed] * 7, [self.joint_current] * 7)
        rospy.sleep(2.0)
        joint_error_ = np.linalg.norm(np.array(self.dual_joint_positions[is_left]) - np.array(left_target_joint_))
        rospy.loginfo(f"Arm Initialization Status: {joint_error_ < self.joint_tolerance}, dual joint error: {joint_error_:.4f}")
        return joint_error_ < self.joint_tolerance

    def move_dual_arm_by_xyz(self, left_target_pos, left_target_quat, right_target_pos, right_target_quat):
        # left_target_joint_ = self.arm_kinematics[True].inverse_kinematics(left_target_pos, left_target_quat, self.dual_joint_positions[True])
        # right_target_joint_ = self.arm_kinematics[False].inverse_kinematics(right_target_pos, right_target_quat, self.dual_joint_positions[False])
        left_target_joint_, right_target_joint_ = self.ik_dual(left_target_pos, left_target_quat, self.dual_joint_positions[True], right_target_pos, right_target_quat, self.dual_joint_positions[False])
        self.send_arms_cmd_pos_service(self.joint_names[True] + self.joint_names[False], list(left_target_joint_) + list(right_target_joint_), [self.joint_speed] * 14, [self.joint_current] * 14)
        # rospy.sleep(3.0)
        left_true_error2_ = np.linalg.norm(np.array(left_target_pos) - np.array(self.left_end_effector_pose))  # 左臂实际末端位置误差
        right_true_error2_ = np.linalg.norm(np.array(right_target_pos) - np.array(self.right_end_effector_pose))  # 右臂实际末端位置误差
        left_arm_move_status, right_arm_move_status = left_true_error2_ < self.joint_tolerance, right_true_error2_ < self.joint_tolerance
        rospy.loginfo("Arm Move Success") if left_arm_move_status and right_arm_move_status else rospy.loginfo("Arm Move Failed")
        rospy.logerr(f"left_true_error2_ = {left_true_error2_}, right_true_error2_ = {right_true_error2_}")
        return left_true_error2_ < self.joint_tolerance, right_true_error2_ < self.joint_tolerance

    def move_single_forward(self, is_left: bool, distance: float) -> bool:
        """移动单只臂"""
        start_pos_, start_rot_, start_quat_ = self.arm_kinematics[is_left].forward_kinematics(self.dual_joint_positions[is_left])
        left_target_pos_ = start_pos_ + distance * np.array([1, 0, 0])  # 向前移动
        move_forward_single_success = self.move_single_arm_by_xyz(is_left, left_target_pos_, start_quat_)
        rospy.loginfo("Move Single Arm Forward Success") if move_forward_single_success else rospy.loginfo("Move Single Arm Forward Failed")
        return move_forward_single_success
    
    def move_single_backward(self, is_left: bool, distance: float) -> bool:
        """移动单只臂"""
        start_pos_, start_rot_, start_quat_ = self.arm_kinematics[is_left].forward_kinematics(self.dual_joint_positions[is_left])
        left_target_pos_ = start_pos_ - distance * np.array([1, 0, 0])  # 向后移动
        move_backward_single_success = self.move_single_arm_by_xyz(is_left, left_target_pos_, start_quat_)
        rospy.loginfo("Move Single Arm Backward Success") if move_backward_single_success else rospy.loginfo("Move Single Arm Backward Failed")
        return move_backward_single_success
    
    def move_single_up(self, is_left: bool, distance: float) -> bool:
        """移动单只臂"""
        start_pos_, start_rot_, start_quat_ = self.arm_kinematics[is_left].forward_kinematics(self.dual_joint_positions[is_left])
        left_target_pos_ = start_pos_ + distance * np.array([0, 0, 1])  # 向上移动
        move_up_single_success = self.move_single_arm_by_xyz(is_left, left_target_pos_, start_quat_)
        rospy.loginfo("Move Single Arm Up Success") if move_up_single_success else rospy.loginfo("Move Single Arm Up Failed")
        return move_up_single_success

    def move_single_down(self,  is_left: bool, distance: float) -> bool:
        """移动单只臂"""
        start_pos_, start_rot_, start_quat_ = self.arm_kinematics[is_left].forward_kinematics(self.dual_joint_positions[is_left])
        left_target_pos_ = start_pos_ - distance * np.array([0, 0, 1])  # 向下移动
        move_down_single_success = self.move_single_arm_by_xyz(is_left, left_target_pos_, start_quat_)
        rospy.loginfo("Move Single Arm Down Success") if move_down_single_success else rospy.loginfo("Move Single Arm Down Failed")
        return move_down_single_success
    
    def move_single_left(self, is_left: bool, distance: float) -> bool:
        """移动单只臂"""
        start_pos_, start_rot_, start_quat_ = self.arm_kinematics[is_left].forward_kinematics(self.dual_joint_positions[is_left])
        left_target_pos_ = start_pos_ + distance * np.array([0, 1, 0])  # 向左移动
        move_left_single_success = self.move_single_arm_by_xyz(is_left, left_target_pos_, start_quat_)
        rospy.loginfo("Move Single Arm Left Success") if move_left_single_success else rospy.loginfo("Move Single Arm Left Failed")
        return move_left_single_success
    
    def move_single_right(self, is_left: bool, distance: float) -> bool:
        """移动单只臂"""
        start_pos_, start_rot_, start_quat_ = self.arm_kinematics[is_left].forward_kinematics(self.dual_joint_positions[is_left])
        left_target_pos_ = start_pos_ - distance * np.array([0, 1, 0])  # 向右移动
        move_right_single_success = self.move_single_arm_by_xyz(is_left, left_target_pos_, start_quat_)
        rospy.loginfo("Move Single Arm Right Success") if move_right_single_success else rospy.loginfo("Move Single Arm Right Failed")
        return move_right_single_success

    def move_dual_forward(self, distance: float):
        """前进指定距离"""
        rospy.logerr(f"self.left_joint_positions = {self.left_joint_positions}")
        rospy.logerr(f"self.left_joint_positions = {self.right_joint_positions}")
        left_start_pos_, left_start_rot_, left_start_quat_ = self.arm_kinematics[True].forward_kinematics(self.left_joint_positions)
        right_start_pos_, right_start_rot_, right_start_quat_ = self.arm_kinematics[False].forward_kinematics(self.right_joint_positions)
        # 计算新的目标位置
        left_target_pos_ = left_start_pos_ + distance * np.array([1, 0, 0])  # 向前移动
        right_target_pos_ = right_start_pos_ + distance * np.array([1, 0, 0])  # 向前移动
        rospy.logerr(f"left_target_pos_ = {left_target_pos_}")
        rospy.logerr(f"right_target_pos_ = {right_target_pos_}")
        # 使用相同的姿态
        left_target_quat_ = left_start_quat_
        right_target_quat_ = right_start_quat_
        # 执行移动
        move_forward_left_success, move_forward_right_success = self.move_dual_arm_by_xyz(left_target_pos_, left_target_quat_, right_target_pos_, right_target_quat_)
        # 打印调试信息
        rospy.loginfo("Moved forward successfully") if move_forward_left_success and move_forward_right_success else rospy.logerr("Failed to move forward")
        return move_forward_left_success and move_forward_right_success

    def move_dual_backward(self, distance: float) -> bool:
        """后退指定距离"""
        left_start_pos_, left_start_rot_, left_start_quat_ = self.arm_kinematics[True].forward_kinematics(self.left_joint_positions)
        right_start_pos_, right_start_rot_, right_start_quat_ = self.arm_kinematics[False].forward_kinematics(self.right_joint_positions)
        # 计算新的目标位置
        left_target_pos_ = left_start_pos_ - distance * np.array([1, 0, 0])  # 向后移动
        right_target_pos_ = right_start_pos_ - distance * np.array([1, 0, 0])  # 向后移动
        # 使用相同的姿态
        left_target_quat_ = left_start_quat_
        right_target_quat_ = right_start_quat_
        # 执行移动
        move_backward_left_success, move_backward_right_success = self.move_dual_arm_by_xyz(left_target_pos_, left_target_quat_, right_target_pos_, right_target_quat_)
        # 打印调试信息
        rospy.loginfo("Moved backward successfully") if move_backward_left_success and move_backward_right_success else rospy.logerr("Moved backward Failed")
        return move_backward_left_success and move_backward_right_success

    def move_dual_up(self, distance: float) -> bool:
        """向上移动指定距离"""
        # 计算新的目标位置
        left_target_pos_ = self.left_end_effector_pose + distance * np.array([0, 0, 1])  # 向上移动
        print("left_end_effector_pose", self.left_end_effector_pose)
        print("left_target_pos_", left_target_pos_)
        print("left_end_effector_quat", self.left_end_effector_quat)
        right_target_pos_ = self.right_end_effector_pose + distance * np.array([0, 0, 1])  # 向上移动
        print("right_end_effector_pose", self.right_end_effector_pose)
        print("right_target_pos_", right_target_pos_)
        print("right_end_effector_quat", self.right_end_effector_quat)
        # 使用相同的姿态
        left_target_quat_ = self.left_end_effector_quat
        right_target_quat_ = self.right_end_effector_quat
        # 执行移动
        move_up_left_success, move_up_right_success = self.move_dual_arm_by_xyz(left_target_pos_, left_target_quat_, right_target_pos_, right_target_quat_)
        rospy.loginfo("Moved up successfully") if move_up_left_success and move_up_right_success else rospy.logerr("Moved up Failed")
        # 打印调试信息
        return move_up_left_success and move_up_right_success

    def move_dual_down(self, distance: float) -> bool:
        """向下移动指定距离"""
        left_start_pos_, left_start_rot_, left_start_quat_ = self.arm_kinematics[True].forward_kinematics(self.left_joint_positions)
        right_start_pos_, right_start_rot_, right_start_quat_ = self.arm_kinematics[False].forward_kinematics(self.right_joint_positions)
        # 计算新的目标位置
        left_target_pos_ = left_start_pos_ - distance * np.array([0, 0, 1])  # 向下移动
        right_target_pos_ = right_start_pos_ - distance * np.array([0, 0, 1])  # 向下移动
        # 使用相同的姿态
        left_target_quat_ = left_start_quat_
        right_target_quat_ = right_start_quat_
        # 执行移动
        move_down_left_success, move_down_right_success = self.move_dual_arm_by_xyz(left_target_pos_, left_target_quat_, right_target_pos_, right_target_quat_)
        # 打印调试信息
        rospy.loginfo("Moved down successfully") if move_down_left_success and move_down_right_success else rospy.logerr("Moved down Failed")
        return move_down_left_success and move_down_right_success

    def move_dual_left(self, distance: float):
        """向左移动指定距离"""
        left_start_pos_, left_start_rot_, left_start_quat_ = self.arm_kinematics[True].forward_kinematics(self.left_joint_positions)
        right_start_pos_, right_start_rot_, right_start_quat_ = self.arm_kinematics[False].forward_kinematics(self.right_joint_positions)
        # 计算新的目标位置
        left_target_pos_ = left_start_pos_ - distance * np.array([0, 1, 0])
        right_target_pos_ = right_start_pos_ - distance * np.array([0, 1, 0])
        # 使用相同的姿态
        left_target_quat_ = left_start_quat_
        right_target_quat_ = right_start_quat_
        # 执行移动
        move_left_success, move_right_success = self.move_dual_arm_by_xyz(left_target_pos_, left_target_quat_, right_target_pos_, right_target_quat_)
        rospy.loginfo("Moved left successfully") if move_left_success and move_right_success else rospy.logerr("Moved left Failed")
        return move_left_success and move_right_success

    def move_dual_right(self, distance: float):
        """向右移动指定距离"""
        left_start_pos_, left_start_rot_, left_start_quat_ = self.arm_kinematics[True].forward_kinematics(self.left_joint_positions)
        right_start_pos_, right_start_rot_, right_start_quat_ = self.arm_kinematics[False].forward_kinematics(self.right_joint_positions)
        # 计算新的目标位置
        left_target_pos_ = left_start_pos_ + distance * np.array([0, 1, 0])
        right_target_pos_ = right_start_pos_ + distance * np.array([0, 1, 0])
        # 使用相同的姿态
        left_target_quat_ = left_start_quat_
        right_target_quat_ = right_start_quat_
        # 执行移动
        move_right_success, move_left_success = self.move_dual_arm_by_xyz(left_target_pos_, left_target_quat_, right_target_pos_, right_target_quat_)
        rospy.loginfo("Moved right successfully") if move_right_success and move_left_success else rospy.logerr("Moved right Failed")
        return move_right_success and move_left_success

    def move_dual(self, distance: float, direction: str):
        """根据方向移动指定距离"""
        if direction == "forward":
            return self.move_dual_forward(distance)
        elif direction == "backward":
            return self.move_dual_backward(distance)
        elif direction == "up":
            return self.move_dual_up(distance)
        elif direction == "down":
            return self.move_dual_down(distance)
        elif direction == "left":
            return self.move_dual_left(distance)
        elif direction == "right":
            return self.move_dual_right(distance)
        else:
            rospy.logerr(f"Invalid direction: {direction}. Use 'forward', 'backward', 'up', or 'down'.")
            return False

    def init_arm_status(self):
        rospy.loginfo("Arm Controller Initializing...")
        all_joint_names = self.joint_names[True] + self.joint_names[False]
        all_positions = [0.0] * 14  # 初始化所有关节位置为0
        all_positions[1] = 0.15
        all_positions[8] = -0.15
        # all_positions[3] = -1.57
        # all_positions[10] = -1.57
        speeds = [self.joint_speed] * 14
        currents = [self.joint_current] * 14
        self.send_arms_cmd_pos(all_joint_names, all_positions, speeds, currents)
        rospy.sleep(2.5)
        dual_joint_error_ = np.linalg.norm(np.array(self.left_joint_positions + self.right_joint_positions) - np.array(all_positions))
        rospy.loginfo(f"Arm Initialization Status: {dual_joint_error_ < self.joint_tolerance}, dual joint error: {dual_joint_error_:.4f}")
        return dual_joint_error_ < self.joint_tolerance

    def control_loop(self):
        left_target_pos_ = [0.28298403, 0.24302717, 0.06437022]
        left_target_quat_ = [0.706715, 0.03085568, -0.70615245, -0.03083112]
        right_target_pos_ = [0.28298403, -0.18722009, 0.05216848]
        right_target_quat_ = [0.706715, 0.03085568, -0.70615245, -0.03083112]
        success_left, success_right = self.move_dual_arm_by_xyz(left_target_pos_, left_target_quat_, right_target_pos_, right_target_quat_)
        return success_left and success_right
    
    def run(self):
        """启动控制器"""
        try:
            rospy.loginfo("Starting dual arm control loop")
            self.control_loop()
        except rospy.ROSInterruptException:
            rospy.logerr("Dual arm controller interrupted")


if __name__ == "__main__":
    rospy.init_node("ArmController")
    controller = ArmController()
    controller.run()
    rospy.spin()
