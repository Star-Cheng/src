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
from solver.dual.dual_arm_solver import ArmKinematics
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
from solver.tracik.arm_solver_que import ArmTracIKSolver
from solver.mix.my_solver import MySolver
from controllers.pid_controller import PIDController as PID


class ArmController:
    def __init__(self):
        # 左右臂运动学求解器
        # self.arm_left_kinematics = ArmKinematics(True)
        # self.arm_right_kinematics = ArmKinematics(False)
        self.arm_left_kinematics = ArmTracIKSolver("./ur_record/urdf/robot.urdf", "pelvis", "wrist_roll_l_link")
        self.arm_right_kinematics = ArmTracIKSolver("./ur_record/urdf/robot.urdf", "pelvis", "wrist_roll_r_link")
        self.arm_kinematics = [self.arm_right_kinematics, self.arm_left_kinematics]
        self.mirror_ls = [1, -1, -1, 1, -1, 1, -1]
        # 控制器参数
        self.joint_speed = rospy.get_param("~joint_speed", 150)  # rpm
        self.joint_current = rospy.get_param("~joint_current", 80.0)  # A
        self.joint_tolerance = rospy.get_param("~joint_tolerance", 0.01)  # rad
        self.tr_distance = rospy.get_param("~tr_distance", 0.05)  # m
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
        self.left_end_effector_pose, self.right_end_effector_rota, self.left_end_effector_quat = self.arm_left_kinematics.forward_kinematics(self.left_joint_positions)
        self.right_end_effector_pose, self.right_end_effector_rota, self.right_end_effector_quat = self.arm_right_kinematics.forward_kinematics(self.right_joint_positions)
        self.joint_names = {True: [i for i in range(11, 18)], False: [j for j in range(21, 28)]}
        # PID控制器参数 (需根据实际系统调整)
        self.pid_kp = rospy.get_param("~pid_kp", 0.5)
        self.pid_ki = rospy.get_param("~pid_ki", 0.01)
        self.pid_kd = rospy.get_param("~pid_kd", 0.1)
        self.pid_min_output = rospy.get_param("~pid_min_output", 0.0)  # 最小输出限制
        self.pid_max_output = rospy.get_param("~pid_max_output", self.joint_current)   # 最大输出限制
        # 为每个关节创建PID控制器 (14个关节)
        self.pid_controllers = {}
        joint_ids = self.joint_names[True] + self.joint_names[False]
        for jid in joint_ids:
            self.pid_controllers[jid] = PID(
                kp=self.pid_kp,
                ki=self.pid_ki,
                kd=self.pid_kd,
                min=self.pid_min_output,
                max=self.pid_max_output
            )
        
        # 控制频率 (Hz)
        self.control_freq = rospy.get_param("~control_freq", 50.0)
        self.control_period = rospy.Duration(1.0 / self.control_freq)
        
        # 创建控制定时器
        self.control_timer = rospy.Timer(self.control_period, self.control_loop_callback)
        
        # PID控制器启用标志
        self.pid_enabled = True
        self.current_targets = {}  # {关节ID: 目标位置}
        self.last_positions = {}   # 用于计算速度的上一位置
        # 设置订阅者
        rospy.Subscriber("/arm/status", MotorStatusMsg, self.arm_status_callback)
        # 设置发布者
        self.arm_cmd_pos_pub = rospy.Publisher("/arm/cmd_pos", CmdSetMotorPosition, queue_size=10)
        # 初始化关节状态
        # self.init_arm_status()
        rospy.sleep(0.1)

    def set_pid_enabled(self, enable: bool):
        """启用/禁用PID控制器"""
        self.pid_enabled = enable
        if enable:
            rospy.loginfo("PID control enabled")
        else:
            rospy.logwarn("PID control disabled")

    def reset_pid_controllers(self):
        """重置所有PID控制器状态"""
        for pid in self.pid_controllers.values():
            pid.reset()
        rospy.loginfo("All PID controllers reset")

    def control_loop_callback(self, event):
        """PID控制循环定时器回调"""
        if not self.pid_enabled or not self.current_targets:
            return
            
        current_time = time.time()
        
        with self.lock:
            cmd_msgs_ = CmdSetMotorPosition()
            cmd_msgs_.header = Header(stamp=rospy.Time.now(), frame_id="")
            
            # 处理每个有目标位置的关节
            for jid, target_info in self.current_targets.items():
                # 获取当前关节位置
                if jid not in self.left_joint_status["name"]:
                    continue
                    
                idx = self.left_joint_status["name"].index(jid)
                current_pos = self.left_joint_status["pos"][idx]
                
                # 获取PID控制器
                pid = self.pid_controllers[jid]
                
                # 计算位置误差
                error = target_info['position'] - current_pos
                
                # 计算PID输出 (控制作用)
                try:
                    # 尝试获取上一位置计算速度
                    last_pos = self.last_positions.get(jid, current_pos)
                    dt = 1.0 / self.control_freq
                    velocity = (current_pos - last_pos) / dt
                except:
                    velocity = 0.0
                    
                control_output = pid.update(
                    error, 
                    derivative=velocity,  # 使用实际速度作为微分项
                    current_time=current_time
                )
                
                # 使用PID输出作为电流控制
                # (实际实现需根据驱动器调整)
                control_current = min(
                    max(
                        target_info['current'] * (1 + control_output),
                        self.pid_min_output
                    ),
                    self.pid_max_output
                )
                
                # 创建控制命令
                cmd = SetMotorPosition(
                    name=jid,
                    pos=target_info['position'],  # 目标位置
                    spd=target_info['speed'],     # 最大速度限制
                    cur=control_current           # PID调整后的电流
                )
                cmd_msgs_.cmds.append(cmd)
                
                # 更新上一位置
                self.last_positions[jid] = current_pos
                
            # 发布PID控制命令
            if cmd_msgs_.cmds:
                self.arm_cmd_pos_pub.publish(cmd_msgs_)

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
                right_joints[i]= left_joints[i] * self.mirror_ls[i]
        else:
            for i in range(len(left_joints)):
                left_joints[i]= right_joints[i] * self.mirror_ls[i]
        return left_joints, right_joints

    def arm_status_callback(self, arm_status_msg: MotorStatusMsg):
        # with self.lock:
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
        self.left_end_effector_pose, self.left_end_effector_rota, self.left_end_effector_quat = self.arm_left_kinematics.forward_kinematics(self.left_joint_positions)
        self.right_end_effector_pose, self.right_end_effector_rota, self.right_end_effector_quat = self.arm_right_kinematics.forward_kinematics(self.right_joint_positions)
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
        if not self.pid_enabled:
            # 创建命令消息
            cmd_msgs_ = CmdSetMotorPosition()
            cmd_msgs_.header = Header(stamp=rospy.Time.now(), frame_id="")
            # 左臂：11---17, 右臂：21---27
            for i in range(len(name_ls)):
                cmd = SetMotorPosition(name=name_ls[i], pos=pos_ls[i], spd=spd_ls[i], cur=cur_ls[i])  # 名称 # 弧度  # RPM  # 安培
                cmd_msgs_.cmds.append(cmd)
            self.arm_cmd_pos_pub.publish(cmd_msgs_)
        else:
            # PID控制模式：设置目标位置
            with self.lock:
                for i, name in enumerate(name_ls):
                    self.current_targets[name] = {
                        'position': pos_ls[i],
                        'speed': spd_ls[i],
                        'current': cur_ls[i]
                    }
                    # 重置该关节的PID控制器
                    self.pid_controllers[name].reset()

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
            for name in name_ls:
                if name in self.current_targets:
                    del self.current_targets[name]

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
        self.send_arms_cmd_pos_service(self.joint_names[True] + self.joint_names[False], left_joints + right_joints, [self.joint_speed] * 14, [self.joint_current] * 14)
        # rospy.sleep(3)
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
        return left_true_error2_ < self.joint_tolerance and right_true_error2_ < self.joint_tolerance

    def move_dual_arm_by_xyz_tr(self, left_target_pos, left_target_quat, right_target_pos, right_target_quat):
        # 分别计算左右臂的轨迹
        # traj_left_joints_ = self.arm_kinematics[True].generate_trajectory_by_dist(self.left_end_effector_pose, left_target_pos, self.tr_distance)
        # traj_right_joints_ = self.arm_kinematics[False].generate_trajectory_by_dist(self.right_end_effector_pose, right_target_pos, self.tr_distance)
        left_start_pos_ = self.arm_kinematics[True].create_pose(self.left_end_effector_pose, self.left_end_effector_quat)
        left_end_pos_ = self.arm_kinematics[True].create_pose(left_target_pos, left_target_quat)
        right_start_pos_ = self.arm_kinematics[False].create_pose(self.right_end_effector_pose, self.right_end_effector_quat)
        right_end_pos_ = self.arm_kinematics[False].create_pose(right_target_pos, right_target_quat)
        traj_left_joints_ = self.arm_kinematics[True].plan(left_start_pos_, left_end_pos_, 10, True, [1., 0., 1.])
        traj_right_joints_ = self.arm_kinematics[False].plan(right_start_pos_, right_end_pos_, 10, True, [1., 0., 1.])
        n_left = len(traj_left_joints_)
        n_right = len(traj_right_joints_)
        n_points = max(n_left, n_right)
        # 轨迹插值同步
        if n_left != n_right:
            # 时间归一化处理
            orig_index_left = np.linspace(0, 1, n_left)
            orig_index_right = np.linspace(0, 1, n_right)
            new_index = np.linspace(0, 1, n_points)

            # 插值处理
            synced_left = []
            for joint_idx in range(7):
                joint_values = [point[joint_idx] for point in traj_left_joints_]
                synced_values = np.interp(new_index, orig_index_left, joint_values)
                synced_left.append(synced_values)
            traj_left_joints_ = np.array(synced_left).T

            synced_right = []
            for joint_idx in range(7):
                joint_values = [point[joint_idx] for point in traj_right_joints_]
                synced_values = np.interp(new_index, orig_index_right, joint_values)
                synced_right.append(synced_values)
            traj_right_joints_ = np.array(synced_right).T
        for i in range(n_points):
            left_target_pos = traj_left_joints_[i]
            right_target_pos = traj_right_joints_[i]
            left_target_joint_, right_target_joint_ = self.ik_dual(left_target_pos, left_target_quat, self.dual_joint_positions[True], right_target_pos, right_target_quat, self.dual_joint_positions[False])
            self.send_arms_cmd_pos_service(self.joint_names[True] + self.joint_names[False], list(left_target_joint_) + list(right_target_joint_), [self.joint_speed] * 14, [self.joint_current] * 14)
            # rospy.sleep(3.0)
            left_true_error2_ = np.linalg.norm(np.array(left_target_pos) - np.array(self.left_end_effector_pose))  # 左臂实际末端位置误差
            right_true_error2_ = np.linalg.norm(np.array(right_target_pos) - np.array(self.right_end_effector_pose))  # 右臂实际末端位置误差
            left_arm_move_status, right_arm_move_status = left_true_error2_ < self.joint_tolerance, right_true_error2_ < self.joint_tolerance
            rospy.loginfo("Arm Move Success") if left_arm_move_status and right_arm_move_status else rospy.loginfo("Arm Move Failed")
            rospy.logerr(f"left_true_error2_ = {left_true_error2_}, right_true_error2_ = {right_true_error2_}")
        return left_true_error2_ < self.joint_tolerance and right_true_error2_ < self.joint_tolerance

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
        left_start_pos_, left_start_rot_, left_start_quat_ = self.arm_kinematics[True].forward_kinematics(self.left_joint_positions)
        right_start_pos_, right_start_rot_, right_start_quat_ = self.arm_kinematics[False].forward_kinematics(self.right_joint_positions)
        # 计算新的目标位置
        left_target_pos_ = left_start_pos_ + distance * np.array([1, 0, 0])  # 向前移动
        right_target_pos_ = right_start_pos_ + distance * np.array([1, 0, 0])  # 向前移动
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


if __name__ == "__main__":
    rospy.init_node("ArmController")
    controller = ArmController()
    rospy.spin()
