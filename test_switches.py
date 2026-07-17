#!/usr/bin/env python3
"""
test_switches.py — verify the two door end-switches on the MOTOR Pi.

TOP    switch -> GPIO 5  (board pin 29)
BOTTOM switch -> GPIO 6  (board pin 31)

Each switch: COM-NO wired between the GPIO pin and GND, internal pull-up.
  idle (open)        -> HIGH -> "OPEN"
  triggered (closed) -> LOW  -> "TRIGGERED"

Run:  python3 test_switches.py     (Ctrl-C to quit)
Press/release each switch by hand and watch the state change.
"""

import RPi.GPIO as GPIO
import time

TOP    = 5    # pin 29
BOTTOM = 6    # pin 31

GPIO.setmode(GPIO.BCM)
GPIO.setup(TOP,    GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(BOTTOM, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def label(pin):
    # LOW = switch closed to GND = triggered
    return "TRIGGERED" if GPIO.input(pin) == GPIO.LOW else "OPEN"


try:
    last = {"TOP": None, "BOTTOM": None}
    print("Reading switches — press each one by hand. Ctrl-C to quit.\n")
    while True:
        cur = {"TOP": label(TOP), "BOTTOM": label(BOTTOM)}
        if cur != last:
            print(f"TOP(GPIO5)={cur['TOP']:<9}  BOTTOM(GPIO6)={cur['BOTTOM']:<9}")
            last = cur
        time.sleep(0.05)

except KeyboardInterrupt:
    print("\nbye")
finally:
    GPIO.cleanup()
