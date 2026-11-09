"""The AIC adapter: the cell runtime as an `aic_model` policy (Gazebo, UR5e, ROS 2 Kilted).

Everything here except `RuntimePolicy` and `ros_port` is ROS-free and tested on any OS; those
two are a thin shell over rclpy that runs only where AIC is installed.
"""
