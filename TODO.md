# SIA Robot

## Basics

- [ ] IK
  - [ ] 继续实现 my_solver3: <https://yuanbao.tencent.com/chat/naQivTmsDa/940dd79d-523b-4f1c-aeb0-eb2c2e969976>
  - [x] PID控制: <https://yuanbao.tencent.com/chat/naQivTmsDa/1f8f95c4-4ed7-46ee-b787-47c5ead07ca6?projectId=938165eacbb549b7b4d506b53db59033>
  - [ ] 力控: <https://yuanbao.tencent.com/chat/naQivTmsDa/1a29c449-7aec-4e23-92cc-ce8b33a20d01?projectId=938165eacbb549b7b4d506b53db59033>
  - [ ] 优化PID
  - [x] 优化逆解算时间

- [x] Mujoco
  - [x] Mujoco Server 配合 Client
  - [x] Mujoco Moveit 联合仿真

- [ ] Moveit
  - [x] 将dual moveit 中 topic 发布成 /arm/cmd_pos: CmdSetMotorPosition
  - [ ] set goal pose的时候需要考虑初始位置
  - [x] 通过 ros-bridge 桥接 将数据发送到ROS1
