#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
'''
@File    :   inspire_hand.py
@Time    :   2025/06/28 19:21:39
@Author  :   StarCheng
@Version :   1.0
@Site    :   https://star-cheng.github.io/Blog/
'''
from bodyctrl_msgs.srv import set_angle, set_angleRequest, set_angleResponse
from sensor_msgs.msg import JointState
import numpy as np
import threading
import rospy
import time

class InspireHandController:
    def __init__(self, is_left=True):
        """
        Inspire Hand ROS控制器
        :param hand: 'left' 或 'right'，指定控制的手
        """
        self.hand = 'left' if is_left else 'right'
        self.current_state = None
        self.name = None
        self.position = None
        self.velocity = None
        self.effort = None
        self.joint_tolerance = rospy.get_param("~hand_tolerance", 0.01)
        
        # 状态订阅者
        self.state_sub = rospy.Subscriber(
            f"/inspire_hand/state/{self.hand}_hand",
            JointState,
            self.state_callback
        )
        # 线程同步工具
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        
        # 初始化服务代理（等待服务就绪）
        # rospy.wait_for_service(f"/inspire_hand/set_angle/{self.hand}_hand")

        # 角度设置服务代理
        self.set_angle_service = rospy.ServiceProxy(f"/inspire_hand/set_angle/{self.hand}_hand",set_angle)
        rospy.sleep(0.1)

    # def state_callback(self, msg:JointState):
    #     """处理关节状态更新"""
    #     self.current_state = msg
    #     self.name = msg.name
    #     self.position = msg.position
    #     self.velocity = msg.velocity
    #     self.effort = msg.effort

    def state_callback(self, msg:JointState):
        """处理关节状态更新"""
        with self.lock:  # 同步访问条件变量
            self.current_state = msg
            self.name = msg.name
            self.position = msg.position
            self.velocity = msg.velocity
            self.effort = msg.effort
            
            # 通知等待条件变量的线程
            self.condition.notify_all()

    # def get_hand_state(self) -> tuple:
    #     """ 获取手的当前状态
    #     :return: (name, position, velocity, effort) 分别对应关节名称、位置、速度和力矩
    #     """
    #     if self.current_state is None:
    #         rospy.logwarn("Hand No state data received yet")
    #     return self.name, self.position, self.velocity, self.effort

    def get_hand_state(self) -> tuple:
        """获取手的当前状态（线程安全版本）"""
        with self.lock:  # 获取锁以保证状态一致性
            if self.current_state is None:
                rospy.logwarn("Hand No state data received yet")
                return None, None, None, None
            return self.name, self.position, self.velocity, self.effort

    # def get_finger_state(self, finger_idx) -> tuple:
    #     """
    #     获取手指的当前状态
    #     :param finger_idx: 手指索引 (0-5: 0小指、1-无名指、2-中指、3-食指、4-拇指弯曲、5-拇指旋转)
    #     :return: (position, velocity, effort) 单位均为百分比
    #     """
    #     if self.current_state is None:
    #         rospy.logwarn("Finger No state data received yet")
    #         return None, None, None
    #     return self.position[finger_idx], self.velocity[finger_idx], self.effort[finger_idx]
    
    def get_finger_state(self, finger_idx) -> tuple:
        """
        获取手指的当前状态
        :param finger_idx: 手指索引 (0-5: 0小指、1-无名指、2-中指、3-食指、4-拇指弯曲、5-拇指旋转)
        :return: (position, velocity, effort) 单位均为百分比
        """
        with self.lock:  # 获取锁保证状态一致性
            # 验证索引范围
            if not 0 <= finger_idx <= 5:
                rospy.logerr(f"Invalid finger index {finger_idx}, must be 0-5")
                return None, None, None
                
            # 状态未初始化检查
            if self.current_state is None:
                rospy.logwarn("Finger No state data received yet")
                return None, None, None
                
            # 返回对应手指状态
            return self.position[finger_idx], self.velocity[finger_idx], self.effort[finger_idx]
            

    def set_angles(self, angles:list) -> bool:
        """
        设置手指角度
        :param angles: 长度为6的列表, 对应各手指角度百分比[0.0-1.0]
                       [小指, 无名指, 中指, 食指, 拇指弯曲, 拇指旋转]
        :return: 是否设置成功
        """
        try:
            req = set_angleRequest()
            # 假设角度设置服务的参数结构与力矩设置类似
            if len(angles) != 6:
                rospy.logerr("Angles list must contain exactly 6 values")
                return False
            req.angle0Ratio = angles[0]  # 小指
            req.angle1Ratio = angles[1]  # 无名指
            req.angle2Ratio = angles[2]  # 中指
            req.angle3Ratio = angles[3]  # 食指
            req.angle4Ratio = angles[4]  # 拇指弯曲
            req.angle5Ratio = angles[5]  # 拇指旋转
            
            resp = self.set_angle_service(req)
            rospy.loginfo(f"Angle set {'suceess' if resp.angle_accepted else 'failed'}")
            return resp.angle_accepted
        except rospy.ServiceException as e:
            rospy.logerr(f"Angle set service call failed: {e}")
            return False

    def set_angle_api(self, angles: list) -> bool:
        """
        服务版本的灵巧手控制
        - 发送命令后等待关节到达目标位置(或超时3秒)
        - 返回是否成功到达
        """
        # 1. 发送角度设置命令
        if not self.set_angles(angles):
            rospy.logerr("Failed to send angle command")
            return False
        
        # 2. 准备等待条件
        start_time = time.time()
        timeout = 2.5  # 2.5秒超时
        success = False
            
        with self.condition:
            # 循环检查直到超时或成功
            while not rospy.is_shutdown() and (time.time() - start_time) < timeout:
                # 检查当前位置状态
                if self.position is None:
                    rospy.logwarn("Waiting for initial joint state...")
                    self.condition.wait(timeout - (time.time() - start_time))
                    continue
                    
                # 检查所有关节是否到达目标
                all_reached = True
                for i in range(6):
                    if abs(self.position[i] - angles[i]) > self.joint_tolerance:
                        all_reached = False
                        break
                
                if all_reached:
                    success = True
                    break
                    
                # 等待下一个状态更新
                remaining = timeout - (time.time() - start_time)
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
        
        # 最终状态检查（处理最后一次更新刚好到达的情况）
        if not success and self.position is not None:
            all_reached = True
            for i in range(6):
                if abs(self.position[i] - angles[i]) > self.joint_tolerance:
                    all_reached = False
                    break
            success = all_reached
        
        # 记录和返回结果
        if success:
            rospy.loginfo(f"All joints reached target in {time.time()-start_time:.2f}s")
        else:
            # 生成详细的关节误差信息
            err_msgs = []
            if self.position:
                for i, name in enumerate(['小指', '无名指', '中指', '食指', '拇指弯曲', '拇指旋转']):
                    error = abs(self.position[i] - angles[i])
                    if error > self.joint_tolerance:
                        err_msgs.append(f"{name}: 目标={angles[i]:.3f} 实际={self.position[i]:.3f}")
            
            rospy.logwarn("Joint timeout: " + ("No state received" if not self.position else 
                        f"{len(err_msgs)} joints not reached\n" + "\n".join(err_msgs)))
        
        return success


# 使用示例
if __name__ == "__main__":
    rospy.init_node('inspire_hand_controller')
    # 创建右手控制器
    right_hand = InspireHandController(is_left=False)
    # 获取五指状态
    name_ls, pos_ls,speed_ls,effort_ls = right_hand.get_hand_state()
    # 获取手指状态
    pos, speed, effort = right_hand.get_finger_state(0)  # 获取小指状态
    # 设置手指角度
    angles = [0.8, 0.8, 0.8, 0.8, 0.1, 0.01]  # 80%-90%打开
    # angles = [1.0, 1.0, 1.0, 1.0, 1.0, 0.01]
    success = right_hand.set_angle_api(angles)
    print("--------------------------")
    rospy.loginfo(f"Set angles {'succeeded' if success else 'failed'}")
    rospy.spin()
