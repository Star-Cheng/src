#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import rospy
import cv2
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError

# 彩色图像回调函数
def Cam_RGB_Callback(msg):
    bridge = CvBridge()
    cv_image = bridge.imgmsg_to_cv2(msg, "bgr8")
    # 弹出窗口显示图片
    cv2.imshow("Image window", cv_image)
    cv2.waitKey(1)

# 主函数
if __name__ == "__main__":
    rospy.init_node("demo_cv_image")
    # 订阅机器人视觉传感器Kinect2的图像话题
    rgb_sub = rospy.Subscriber("/kinect2/hd/image_color_rect",Image,Cam_RGB_Callback,queue_size=10)
    rospy.spin()

# roslaunch wpr_simulation wpb_balls.launch
# rosrun image_pkg image_node.py
# rosrun wpr_simulation ball_random_move