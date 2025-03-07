#!/usr/bin/env python3
# coding=utf-8

import rospy
import cv2
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
import matplotlib.pyplot as plt
# 彩色图像回调函数
def Cam_RGB_Callback(msg):
    bridge = CvBridge()
    cv_image = bridge.imgmsg_to_cv2(msg, "bgr8")
    #转换为灰度图
    gray_img=cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    #创建一个级联分类器
    face_casecade = cv2.CascadeClassifier('/home/gym/code/catkin_ws/src/wpb_home/wpb_home_python/config/haarcascade_frontalface_alt.xml')
    #人脸检测
    face = face_casecade.detectMultiScale(gray_img, 1.3, 5)
    for (x,y,w,h) in face:
        #在原图上绘制矩形
        cv2.rectangle(cv_image,(x,y),(x+w,y+h),(0,0,255),3)
    # 弹出窗口显示图片
    cv2.imshow("face window", cv_image)
    cv2.waitKey(1)

# 主函数
if __name__ == "__main__":
    rospy.init_node("demo_cv_face_detect")
    # 订阅机器人视觉传感器Kinect2的图像话题
    rgb_sub = rospy.Subscriber("/kinect2/hd/image_color_rect",Image,Cam_RGB_Callback,queue_size=10)
    rospy.spin()

# roslaunch wpr_simulation wpb_single_face.launch
# rosrun image_pkg face_node.py
# rosrun wpr_simulation keyboard_vel_ctrl