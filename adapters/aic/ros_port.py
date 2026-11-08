"""`NodePort` over rclpy: the aic_model node's clock, its TF buffer, two subscriptions and the
`move_robot` callback. ROS-only; runs where AIC is installed (not tested on CI).

Callbacks run on aic_model's MultiThreadedExecutor while the policy thread reads. The controller
state keeps only its newest message behind a lock; every wrench goes, tared with the newest tare,
into a bounded `WrenchFeed` that the policy body drains at step ends (ADR 013).

The goal handle is the one aic_model stores on the node before it starts the policy thread
(`aic_model.py:179-181` @ `3ab0653`). It is read once, here: after a cancel the node's execute
loop sets its attribute back to None (`:281-307`), so it cannot be read later.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from action_msgs.msg import GoalStatus
from aic_control_interfaces.msg import ControllerState, MotionUpdate, TrajectoryGenerationMode
from geometry_msgs.msg import Point, Pose, Quaternion, Twist, Vector3, Wrench, WrenchStamped
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.time import Time
from std_msgs.msg import Header
from tf2_ros import TransformException

from adapters.aic import convert
from adapters.aic.force import WrenchDrain, WrenchFeed
from adapters.aic.node import ControllerSnapshot, WrenchReading

WRENCH_TOPIC = "/fts_broadcaster/wrench"
CONTROLLER_STATE_TOPIC = "/aic_controller/controller_state"
QOS_DEPTH = 5  # as aic_adapter subscribes to the wrench (aic_adapter.cpp:71-72)


class RosNodePort:
    def __init__(self, node: Any, move_robot: Any, send_feedback: Any) -> None:
        self._node = node
        self._move_robot = move_robot
        self._send_feedback = send_feedback
        goal = getattr(node, "goal_handle", None)
        readable = all(hasattr(goal, a) for a in ("status", "is_active"))
        # Without a readable handle or the node's lifecycle flag a cancel cannot be seen: the
        # run fails fast as `aic_goal_handle_unavailable`.
        self._goal = goal if readable and hasattr(node, "is_active") else None
        self._tf = node._tf_buffer  # the TF buffer aic_model's Policy examples use
        self._lock = threading.Lock()
        self._wrenches = WrenchFeed()
        self._state: ControllerSnapshot | None = None
        # One mutually exclusive group per feed: callbacks of one feed never overlap, so an
        # older message cannot overwrite a newer one (controller_state is 500 Hz).
        feeds = [
            (WrenchStamped, WRENCH_TOPIC, self._on_wrench),
            (ControllerState, CONTROLLER_STATE_TOPIC, self._on_state),
        ]
        self._subs: list[Any] = []
        try:
            for msg_type, topic, callback in feeds:
                group = MutuallyExclusiveCallbackGroup()
                self._subs.append(
                    node.create_subscription(
                        msg_type, topic, callback, QOS_DEPTH, callback_group=group
                    )
                )
        except Exception:
            self.close()  # do not leave the first subscription behind
            raise

    def close(self) -> None:
        subs, self._subs = self._subs, []
        for sub in subs:
            self._node.destroy_subscription(sub)

    # NodePort -------------------------------------------------------------------------------

    def sim_now_s(self) -> float:
        return self._node.get_clock().now().nanoseconds / 1e9

    def wall_now_s(self) -> float:
        return time.monotonic()

    def wall_sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def goal_stop_reason(self) -> str | None:
        goal = self._goal
        if goal is None:
            return "aic_goal_handle_unavailable"
        try:
            # By status, not `is_cancel_requested`: once the node's execute loop has called
            # `canceled()` (`aic_model.py:276-277`), that flag reads False again.
            if goal.status in (GoalStatus.STATUS_CANCELING, GoalStatus.STATUS_CANCELED):
                return "goal_cancel_requested"
            if not goal.is_active:  # aborted by cancel_task (`:156-160`), or ended otherwise
                return "goal_aborted"
            if not self._node.is_active:
                return "node_deactivated"
        except Exception:
            return "goal_handle_unreadable"  # mid-run; the clock may still be fine
        return None

    def transform(self, frame: str) -> convert.Pose | None:
        try:
            tf = self._tf.lookup_transform(convert.BASE_FRAME, frame, Time()).transform
        except TransformException:
            return None
        t, r = tf.translation, tf.rotation
        return convert.Pose(position=(t.x, t.y, t.z), orientation=(r.w, r.x, r.y, r.z))

    def drain_wrenches(self) -> WrenchDrain:
        return self._wrenches.drain()

    def controller_state(self) -> ControllerSnapshot | None:
        with self._lock:
            return self._state

    def send(self, motion: convert.MotionCommand) -> None:
        stamp = self._node.get_clock().now().to_msg()
        self._move_robot(motion_update=to_motion_update(motion, stamp=stamp))

    def send_feedback(self, message: str) -> None:
        self._send_feedback(message)

    # callbacks ------------------------------------------------------------------------------

    def _on_wrench(self, msg: WrenchStamped) -> None:
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        f, t = msg.wrench.force, msg.wrench.torque
        reading = WrenchReading(stamp_s=stamp, force=(f.x, f.y, f.z), torque=(t.x, t.y, t.z))
        self._wrenches.on_wrench(reading)

    def _on_state(self, msg: ControllerState) -> None:
        p, q = msg.tcp_pose.position, msg.tcp_pose.orientation
        f, t = msg.fts_tare_offset.wrench.force, msg.fts_tare_offset.wrench.torque
        v = msg.tcp_velocity.linear
        snapshot = ControllerSnapshot(
            tcp=convert.Pose(position=(p.x, p.y, p.z), orientation=(q.w, q.x, q.y, q.z)),
            tare_force=(f.x, f.y, f.z),
            tare_torque=(t.x, t.y, t.z),
            tcp_speed_mm_s=convert.mm(convert.magnitude((v.x, v.y, v.z))),
        )
        with self._lock:
            self._state = snapshot
        self._wrenches.on_tare(snapshot.tare_force, snapshot.tare_torque)


def to_motion_update(motion: convert.MotionCommand, *, stamp: Any) -> MotionUpdate:
    """The `MotionUpdate` for a motion command, field by field (`Policy.set_pose_target`)."""
    header = Header(frame_id=motion.frame_id)
    if stamp is not None:
        header.stamp = stamp
    (x, y, z), (qw, qx, qy, qz) = motion.position_m, motion.orientation
    (vx, vy, vz), (wx, wy, wz) = motion.linear_m_s, motion.angular_rad_s
    mode = (
        TrajectoryGenerationMode.MODE_POSITION
        if motion.mode == "position"
        else TrajectoryGenerationMode.MODE_VELOCITY
    )
    return MotionUpdate(
        header=header,
        pose=Pose(position=Point(x=x, y=y, z=z), orientation=Quaternion(w=qw, x=qx, y=qy, z=qz)),
        velocity=Twist(linear=Vector3(x=vx, y=vy, z=vz), angular=Vector3(x=wx, y=wy, z=wz)),
        target_stiffness=list(motion.stiffness),
        target_damping=list(motion.damping),
        feedforward_wrench_at_tip=Wrench(),
        wrench_feedback_gains_at_tip=list(motion.wrench_feedback_gains),
        trajectory_generation_mode=TrajectoryGenerationMode(mode=mode),
    )
