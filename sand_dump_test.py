#!/usr/bin/env python3
"""
Sand dump platform — bench test script (Pi 3 Model B).

Drives a 12V linear actuator through a 2-channel relay H-bridge:
  Relay 2 (IN2, GPIO 22, board pin 15) -> COM2 = +12V -> extend (rod out)
  Relay 1 (IN1, GPIO 17, board pin 11) -> COM1 = +12V -> retract (rod in)
  (directions swapped in software to match the actuator's wiring)
  Both off -> both COMs on GND -> motor braked

Relay board: jumpers S1/S2 on High, DC+ -> pin 2 (5V), DC- -> pin 6 (GND).
Actuator: red/black on COM1/COM2 (as wired). 12V+ -> NO1+NO2, 12V- -> NC1+NC2.

Usage:
  python3 sand_dump_test.py                 # interactive menu
  python3 sand_dump_test.py --travel 40     # set full-travel time for cycle

Commands at the prompt:
  t          relay click test (run with 12V UNPLUGGED first)
  p          pin trigger test: each pin LOW then HIGH, step by step
  e <sec>    extend for <sec> seconds
  r <sec>    retract for <sec> seconds
  E          extend until you press Enter (measures travel time)
  R          retract until you press Enter (measures travel time)
  c          full dump cycle: extend, hold, retract
  q          quit

Ctrl-C at any time stops the motor.
"""

import argparse
import threading
import time

from gpiozero import OutputDevice

EXTEND_PIN = 22   # board pin 15 -> relay IN2
RETRACT_PIN = 17  # board pin 11 -> relay IN1

DEAD_TIME = 0.3   # seconds both relays off before reversing
MAX_RUN = 90      # hard cap on any single motor run (seconds)
DUTY_CYCLE = 0.20 # actuator manual: 20% duty cycle

extend = OutputDevice(EXTEND_PIN, active_high=True, initial_value=False)
retract = OutputDevice(RETRACT_PIN, active_high=True, initial_value=False)

_lock = threading.Lock()
_last_run_s = 0.0
_last_stop = 0.0


def stop():
    with _lock:
        extend.off()
        retract.off()


def _start(direction):
    """Energise exactly one relay. Never both."""
    stop()
    time.sleep(DEAD_TIME)
    with _lock:
        if direction == "extend":
            retract.off()
            extend.on()
        else:
            extend.off()
            retract.on()


def _duty_check(planned_s):
    """Warn if the motor hasn't rested long enough since the last run."""
    if _last_run_s <= 0:
        return
    rest_needed = _last_run_s * (1 - DUTY_CYCLE) / DUTY_CYCLE
    rested = time.monotonic() - _last_stop
    if rested < rest_needed:
        print(f"  ! duty cycle: last run {_last_run_s:.0f}s, rested {rested:.0f}s, "
              f"manual suggests ~{rest_needed:.0f}s rest. Continuing anyway.")


def run(direction, seconds):
    global _last_run_s, _last_stop
    seconds = min(float(seconds), MAX_RUN)
    _duty_check(seconds)
    print(f"  {direction} for {seconds:.1f}s ... (Ctrl-C to stop)")
    t0 = time.monotonic()
    _start(direction)
    try:
        time.sleep(seconds)
    finally:
        stop()
        _last_run_s = time.monotonic() - t0
        _last_stop = time.monotonic()
    print(f"  stopped after {_last_run_s:.1f}s")


def run_until_enter(direction):
    """Run until Enter is pressed (or MAX_RUN). Prints elapsed time."""
    global _last_run_s, _last_stop
    _duty_check(MAX_RUN)
    print(f"  {direction} — press Enter to stop (auto-stop at {MAX_RUN}s)")
    timer = threading.Timer(MAX_RUN, stop)
    t0 = time.monotonic()
    _start(direction)
    timer.start()
    try:
        input()
    finally:
        timer.cancel()
        stop()
        _last_run_s = time.monotonic() - t0
        _last_stop = time.monotonic()
    print(f"  stopped after {_last_run_s:.1f}s")


def relay_click_test():
    print("  Relay click test. Unplug 12V for the first run.")
    for name, dev in (("Relay 2 / extend / GPIO 22", extend),
                      ("Relay 1 / retract / GPIO 17", retract)):
        print(f"  {name}: ON")
        stop()
        dev.on()
        time.sleep(1.0)
        dev.off()
        print(f"  {name}: OFF")
        time.sleep(0.5)
    print("  Each relay should have clicked once and its LED lit for 1s.")


def pin_trigger_test():
    """Hold each GPIO LOW then HIGH and let the user check the relay LED.

    With jumpers on High: LED off when LOW, LED on (click) when HIGH.
    If it's the other way round, that channel's jumper is on Low.
    """
    print("  Pin trigger test. Keep 12V UNPLUGGED.")
    stop()
    for name, dev in (("IN2 / GPIO 22 / pin 15 / Relay 2 / extend", extend),
                      ("IN1 / GPIO 17 / pin 11 / Relay 1 / retract", retract)):
        print(f"\n  {name}")
        dev.off()
        input("    pin LOW  -> relay should be OFF, LED dark.   Enter to continue")
        dev.on()
        input("    pin HIGH -> relay should be ON, click, LED lit. Enter to continue")
        dev.off()
        print("    pin LOW again (relay off)")
        time.sleep(0.3)
    print("\n  If any relay was ON at LOW and OFF at HIGH, move that jumper to Com-High.")


def dump_cycle(travel, hold):
    print(f"  Dump cycle: extend {travel}s, hold {hold}s, retract {travel * 1.1:.0f}s")
    run("extend", travel)
    print(f"  holding {hold}s ...")
    time.sleep(hold)
    # Retract slightly longer so the internal limit switch always seats it flat.
    run("retract", travel * 1.1)
    print("  cycle complete")


def main():
    ap = argparse.ArgumentParser(description="Sand dump actuator bench test")
    ap.add_argument("--travel", type=float, default=40,
                    help="full extend time in seconds (measure with E)")
    ap.add_argument("--hold", type=float, default=3, help="hold time at top")
    args = ap.parse_args()

    print(__doc__.split("Commands at the prompt:")[1])
    try:
        while True:
            try:
                cmd = input("> ").strip().split()
            except EOFError:
                break
            if not cmd:
                continue
            op = cmd[0]
            try:
                if op == "q":
                    break
                elif op == "t":
                    relay_click_test()
                elif op == "p":
                    pin_trigger_test()
                elif op == "e":
                    run("extend", cmd[1] if len(cmd) > 1 else 2)
                elif op == "r":
                    run("retract", cmd[1] if len(cmd) > 1 else 2)
                elif op == "E":
                    run_until_enter("extend")
                elif op == "R":
                    run_until_enter("retract")
                elif op == "c":
                    dump_cycle(args.travel, args.hold)
                else:
                    print("  unknown command")
            except KeyboardInterrupt:
                stop()
                print("\n  ABORTED — motor stopped")
            except ValueError:
                print("  usage: e <seconds>  /  r <seconds>")
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        extend.close()
        retract.close()
        print("Relays off. Bye.")


if __name__ == "__main__":
    main()
