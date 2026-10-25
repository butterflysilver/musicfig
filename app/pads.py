"""The `gsd-pads` hook (life-ops #75). The pad loop owns the USB pad, so the
CLI (scripts/pi/pads.py, installed as /usr/local/bin/gsd-pads) asks the
running service over its localhost web port instead of opening the pad
itself; the state lives in app.lego (flag + file, enforced in
Dimensions.switch_pad / fade_pad / flash_pad).

  GET  /pads        {"off": bool, "pads": 0|1}
  POST /pads/off    every pad dark until `on`     -> status + "changed": bool
  POST /pads/on     the idle colour back          -> status + "changed": bool

Like the rest of the web app this answers on 127.0.0.1 only (run.py).
"""
from flask import Blueprint, jsonify

import app.lego as lego

pads = Blueprint('pads', __name__)


@pads.route('/pads')
def status():
    return jsonify(lego.pads_status())


@pads.route('/pads/<mode>', methods=['POST'])
def set_mode(mode):
    if mode not in ('on', 'off'):
        return jsonify(error='use POST /pads/on or /pads/off'), 404
    before = lego.pads_off()
    lego.set_pads_off(mode == 'off')
    state = lego.pads_status()
    state['changed'] = state['off'] != before
    return jsonify(state)
