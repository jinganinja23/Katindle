# /home/j/Katindle/gpioB.py
from gpiozero import Button
from time import monotonic

def setup_buttons(app):
    # small helper to stamp the press time, then call your method
    def stamp_and(fn):
        def inner():
            app.last_press_ts = monotonic()   # ← timestamp set HERE
            fn()
        return inner

    btn_up     = Button(14, pull_up=True, bounce_time=0.02)
    btn_down   = Button(4,  pull_up=True, bounce_time=0.02)
    btn_select = Button(3,  pull_up=True, bounce_time=0.02)
    btn_back   = Button(2,  pull_up=True, bounce_time=0.02)

    btn_up.when_pressed     = stamp_and(app.up)
    btn_down.when_pressed   = stamp_and(app.down)
    btn_select.when_pressed = stamp_and(app.select)
    btn_back.when_pressed   = stamp_and(app.back)

    # return so they don't get GC'd
    return [btn_up, btn_down, btn_select, btn_back]
