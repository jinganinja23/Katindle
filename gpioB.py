# /home/j/Katindle/gpioB.py
from gpiozero import Button

def setup_buttons(app):
    btn_up     = Button(14, pull_up=True, bounce_time=0.02)
    btn_down   = Button(4,  pull_up=True, bounce_time=0.02)
    btn_select = Button(3,  pull_up=True, bounce_time=0.02)
    btn_back   = Button(2,  pull_up=True, bounce_time=0.02)

    btn_up.when_pressed     = lambda: app.up()
    btn_down.when_pressed   = lambda: app.down()
    btn_select.when_pressed = lambda: app.select()
    btn_back.when_pressed   = lambda: app.back()

    # return so they don't get GC'd
    return [btn_up, btn_down, btn_select, btn_back]
