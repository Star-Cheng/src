cmake_minimum_required(VERSION 2.8.3)
project(g1_12dof_description)

find_package(catkin REQUIRED)

catkin_package()

find_package(roslaunch)

foreach(dir launch meshes)
  install(DIRECTORY ${dir}/
          DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}/${dir})
endforeach(dir)
