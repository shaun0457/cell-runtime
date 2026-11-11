"""Read one trial's bag and write the referee's data as JSON (ADR 029).

Run with the eval container's own Python, which has rosbag2_py and the message types:
    python3 bag_dump.py <bag directory> <output json>
It imports nothing from this repository, so the repository gains no dependency on ROS.

The scorer records each message with its receive time (`ScoringTier2.cc:115-117`), wall clock.
Kept, each list with a bound:
- `insertions`: `/scoring/insertion_event` (std_msgs/String, no header): the receive time and the
  port name as sent. That is the touch topic's namespace (`CablePlugin.cc:268-269`, `:324`), so
  it may start with `/`; the scorer splits it on `/`, drops empty parts, and reads the first two
  as the target module and the port (`ScoringTier2.cc:812-838`). Every event is kept: the
  scorer counts the last one it receives (`:444-447`);
- `contacts`: `/aic/gazebo/contacts/off_limit` messages with at least one contact: the receive
  time and the count;
- `pairs`: for each `/fts_broadcaster/wrench` message, its receive time and its header stamp
  (sim time), which map the insertion's wall time onto sim time.
A bag past any bound is refused with a fixed message and no output, not cut short. The output
is written to a temporary file and renamed, so a dump killed at its timeout leaves no partial
JSON for the runner to take as done.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

INSERTION_TOPIC = "/scoring/insertion_event"
CONTACTS_TOPIC = "/aic/gazebo/contacts/off_limit"
WRENCH_TOPIC = "/fts_broadcaster/wrench"
# The broadcaster publishes at 50 Hz (aic_bringup/config/aic_ros2_controllers.yaml:16-20):
# about 9,000 pairs in a 180 s trial.
MAX_PAIRS = 100_000
MAX_INSERTIONS = 16
MAX_CONTACT_MSGS = 100_000
MAX_PORT_CHARS = 256

PAIRS_OVER = f"the bag holds more than {MAX_PAIRS} wrench messages"
INSERTIONS_OVER = f"the bag holds more than {MAX_INSERTIONS} insertion events"
CONTACTS_OVER = f"the bag holds more than {MAX_CONTACT_MSGS} contact messages"
PORT_TOO_LONG = f"an insertion event names a port longer than {MAX_PORT_CHARS} characters"

Record = tuple[str, int, object]  # (topic, receive time in ns, the value kept)


class BagRefused(ValueError):
    """A bag this reader will not summarise; the message is one of the fixed ones above."""


def collect(records: Iterable[Record]) -> dict[str, object]:
    """The referee's JSON from (topic, receive ns, value) records, in bag order."""
    insertions: list[dict[str, object]] = []
    contacts: list[dict[str, int]] = []
    pairs: list[list[int]] = []
    for topic, recv_ns, value in records:
        if topic == INSERTION_TOPIC:
            port = str(value)
            if len(port) > MAX_PORT_CHARS:
                raise BagRefused(PORT_TOO_LONG)
            if len(insertions) == MAX_INSERTIONS:
                raise BagRefused(INSERTIONS_OVER)
            insertions.append({"recv_ns": int(recv_ns), "port": port})
        elif topic == CONTACTS_TOPIC:
            count = int(value)  # type: ignore[call-overload]
            if count <= 0:
                continue
            if len(contacts) == MAX_CONTACT_MSGS:
                raise BagRefused(CONTACTS_OVER)
            contacts.append({"recv_ns": int(recv_ns), "count": count})
        elif topic == WRENCH_TOPIC:
            if len(pairs) == MAX_PAIRS:
                raise BagRefused(PAIRS_OVER)
            pairs.append([int(recv_ns), int(value)])  # type: ignore[call-overload]
    return {"version": 1, "insertions": insertions, "contacts": contacts, "pairs": pairs}


def _records(bag: Path) -> Iterator[Record]:  # pragma: no cover - needs rosbag2_py
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag), storage_id=""),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )
    types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    wanted = [t for t in (INSERTION_TOPIC, CONTACTS_TOPIC, WRENCH_TOPIC) if t in types]
    reader.set_filter(rosbag2_py.StorageFilter(topics=wanted))
    classes = {t: get_message(types[t]) for t in wanted}
    while reader.has_next():
        topic, data, recv_ns = reader.read_next()
        msg = deserialize_message(data, classes[topic])
        if topic == INSERTION_TOPIC:
            yield topic, recv_ns, msg.data
        elif topic == CONTACTS_TOPIC:
            yield topic, recv_ns, len(msg.contacts)
        else:
            stamp = msg.header.stamp
            yield topic, recv_ns, stamp.sec * 1_000_000_000 + stamp.nanosec


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 bag_dump.py <bag directory> <output json>", file=sys.stderr)
        return 2
    bag, out = Path(argv[0]), Path(argv[1])
    try:
        summary = collect(_records(bag))
    except BagRefused as ex:
        print(f"bag_dump: {ex}", file=sys.stderr)
        return 1
    except Exception as ex:  # a missing or broken bag: the type only, never its text
        print(f"bag_dump: the bag could not be read ({type(ex).__name__})", file=sys.stderr)
        return 1
    partial = out.with_name(out.name + ".partial")
    partial.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(partial, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
