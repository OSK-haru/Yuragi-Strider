"""Small contract for the scheduled-drive selection exercise."""

import unittest

from core.attack_simulation.active_drive_relations import (
    ScheduledDriveInterval,
    TimeWindow,
    select_active_drive_transfers,
)
from core.experimental_isolation_physics.coherent_crosstalk import (
    DriveCrosstalkTransfer,
)


class ActiveDriveRelationTests(unittest.TestCase):
    def test_time_window_and_drive_validation(self) -> None:
        with self.assertRaises(ValueError):
            TimeWindow(1.0, 1.0)
        with self.assertRaises(ValueError):
            TimeWindow(-1.0, 1.0)
        with self.assertRaises(ValueError):
            TimeWindow(0.0, float("inf"))
        with self.assertRaises(ValueError):
            ScheduledDriveInterval(-1, TimeWindow(0.0, 1.0))

    def test_selector_input_validation(self) -> None:
        with self.assertRaises(TypeError):
            select_active_drive_transfers("wrong", (), ())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            select_active_drive_transfers(TimeWindow(0.0, 1.0), ("wrong",), ())  # type: ignore[arg-type]


    def test_overlap_touching_boundary_and_source_match(self) -> None:
        transfer = DriveCrosstalkTransfer(10, 11, 0.1)
        drive = ScheduledDriveInterval(10, TimeWindow(0.0, 1.0))

        self.assertEqual(
            select_active_drive_transfers(TimeWindow(0.5, 1.5), (drive,), (transfer,)),
            (transfer,),
        )
        self.assertEqual(
            select_active_drive_transfers(TimeWindow(1.0, 2.0), (drive,), (transfer,)),
            (),
        )
        self.assertEqual(
            select_active_drive_transfers(
                TimeWindow(0.0, 1.0),
                (ScheduledDriveInterval(12, TimeWindow(0.0, 1.0)),),
                (transfer,),
            ),
            (),
        )


    def test_zero_transfer_and_repeated_drive_keep_unique_declaration_order(self) -> None:
        first = DriveCrosstalkTransfer(10, 11, 0.1)
        zero = DriveCrosstalkTransfer(10, 12, 0.0)
        second = DriveCrosstalkTransfer(10, 13, 0.2)
        drives = (
            ScheduledDriveInterval(10, TimeWindow(0.0, 2.0)),
            ScheduledDriveInterval(10, TimeWindow(1.0, 3.0)),
        )

        self.assertEqual(
            select_active_drive_transfers(
                TimeWindow(1.0, 2.0), drives, (second, zero, first)
            ),
            (second, first),
        )


if __name__ == "__main__":
    unittest.main()
