from __future__ import annotations
import gremlin
import gremlin.input_devices
from gremlin.user_plugin import *
import logging
import time
import threading

syslog = logging.getLogger("system")

# ============================================================================
# DEVICE DEFINITIONS
# ============================================================================
MFG_Crosswind_V2_NAME = "MFG Crosswind V2"
MFG_Crosswind_V2_GUID = "2f681490ea8a11ed801a444553540000"

MFG_Crosswind_V2_Default = gremlin.input_devices.JoystickDecorator(
    MFG_Crosswind_V2_NAME, 
    MFG_Crosswind_V2_GUID, 
    "Default"
)

# ============================================================================
# SETTINGS
# ============================================================================
vjoy_forward_axis = IntegerVariable("vJoy Forward Axis", "2=Y Forward/Backward", 2, 1, 8)
vjoy_lateral_axis = IntegerVariable("vJoy Lateral Axis", "1=X Left/Right", 1, 1, 8)
sensitivity = FloatVariable("Sensitivity", "Gain", 0.8, 0.01, 5.0)
decay_rate = FloatVariable("Decay Rate", "Friction", 0.95, 0.1, 0.99)

# Run hold settings
run_threshold = FloatVariable("Run Threshold", "Velocity to hold sprint button", 0.7, 0.1, 0.95)
run_duration = FloatVariable("Run Duration", "Time above threshold to trigger (seconds)", 0.2, 0.1, 1.0)
run_button = IntegerVariable("Run Button", "vJoy button to hold", 1, 1, 32)

direction_threshold = FloatVariable("Direction Threshold", "Toe brake value for full lateral movement", 0.8, 0.1, 1.0)

class TreadmillState:
    velocity = 0.0
    last_rudder_pos = 0.0
    vjoy_id = 1
    decay_thread = None
    decay_thread_running = False
    thread_lock = threading.Lock()
    
    # Run hold state
    is_running = False
    above_threshold_time = None
    
    left_brake_value = 0.0  
    right_brake_value = 0.0  

state = TreadmillState()

# ============================================================================
# CORE LOGIC UPDATES
# ============================================================================

def update_run_state(vjoy_handle, current_time):
    """Updates the sprint button state based on current velocity (Hold Logic)"""
    try:
        # Check if we should START holding the sprint button
        if not state.is_running and state.velocity >= run_threshold.value:
            if state.above_threshold_time is None:
                state.above_threshold_time = current_time
            elif current_time - state.above_threshold_time >= run_duration.value:
                state.is_running = True
                vjoy_handle[state.vjoy_id].button(run_button.value).is_pressed = True
                syslog.info(f"Rudder Treadmill: SPRINT HOLD ON (velocity: {state.velocity:.2f})")
        
        # Reset the timer if velocity dips below threshold while not yet sprinting
        elif not state.is_running and state.velocity < run_threshold.value:
            state.above_threshold_time = None

        # Check if we should RELEASE the sprint button
        # We release if velocity drops significantly below the threshold
        if state.is_running and state.velocity < run_threshold.value:
            state.is_running = False
            state.above_threshold_time = None
            vjoy_handle[state.vjoy_id].button(run_button.value).is_pressed = False
            syslog.info(f"Rudder Treadmill: SPRINT HOLD OFF (velocity: {state.velocity:.2f})")
            
    except Exception as e:
        syslog.error(f"Rudder Treadmill: Error updating run button: {str(e)}")

def apply_directional_movement(vjoy_handle):
    if state.velocity <= 0.01:
        try:
            vjoy_handle[state.vjoy_id].axis(vjoy_forward_axis.value).value = 0.0
            vjoy_handle[state.vjoy_id].axis(vjoy_lateral_axis.value).value = 0.0
        except Exception as e:
            syslog.error(f"Rudder Treadmill: Error zeroing vJoy axes: {str(e)}")
        return 0.0, 0.0
    
    left_ratio = min(1.0, state.left_brake_value / direction_threshold.value)
    right_ratio = min(1.0, state.right_brake_value / direction_threshold.value)
    
    forward_value = 0.0
    lateral_value = 0.0
    
    if state.left_brake_value > 0.1 and state.right_brake_value > 0.1:
        backward_intensity = max(left_ratio, right_ratio)
        forward_value = -state.velocity * backward_intensity
        lateral_value = 0.0
    else:
        forward_reduction = max(left_ratio, right_ratio)
        forward_value = state.velocity * (1.0 - forward_reduction)
        
        if state.left_brake_value > 0.1:
            lateral_value = state.velocity * left_ratio
        elif state.right_brake_value > 0.1:
            lateral_value = -state.velocity * right_ratio
        else:
            lateral_value = 0.0
    
    try:
        vjoy_handle[state.vjoy_id].axis(vjoy_forward_axis.value).value = forward_value
        vjoy_handle[state.vjoy_id].axis(vjoy_lateral_axis.value).value = lateral_value
    except Exception as e:
        syslog.error(f"Rudder Treadmill: Error updating vJoy axes: {str(e)}")
    
    return forward_value, lateral_value

def decay_loop(vjoy_handle):
    syslog.info("Rudder Treadmill: Decay thread started")
    
    while state.decay_thread_running and state.velocity > 0:
        current_time = time.time()
        state.velocity *= decay_rate.value
        if state.velocity < 0.01:
            state.velocity = 0.0
        
        apply_directional_movement(vjoy_handle)
        update_run_state(vjoy_handle, current_time)
        time.sleep(0.02)
    
    try:
        vjoy_handle[state.vjoy_id].axis(vjoy_forward_axis.value).value = 0.0
        vjoy_handle[state.vjoy_id].axis(vjoy_lateral_axis.value).value = 0.0
        # Ensure button is released when stopping
        if state.is_running:
            state.is_running = False
            vjoy_handle[state.vjoy_id].button(run_button.value).is_pressed = False
            syslog.info("Rudder Treadmill: SPRINT HOLD OFF (velocity zero)")
    except Exception as e:
        syslog.error(f"Rudder Treadmill: Error cleaning up: {str(e)}")
    
    with state.thread_lock:
        state.decay_thread = None
        state.decay_thread_running = False
    
    syslog.info("Rudder Treadmill: Decay thread stopped")

@MFG_Crosswind_V2_Default.axis(6)
def on_rudder_move(event, vjoy):
    current_time = time.time()
    delta = abs(event.value - state.last_rudder_pos)
    state.last_rudder_pos = event.value
    
    if delta > 0.001:
        state.velocity = min(1.0, state.velocity + (delta * sensitivity.value))
    
    apply_directional_movement(vjoy)
    update_run_state(vjoy, current_time)
    
    if state.velocity > 0:
        with state.thread_lock:
            if not state.decay_thread_running:
                state.decay_thread_running = True
                state.decay_thread = threading.Thread(
                    target=decay_loop, 
                    args=(vjoy,),
                    daemon=True
                )
                state.decay_thread.start()

@MFG_Crosswind_V2_Default.axis(2)
def on_left_brake_move(event, vjoy):
    state.left_brake_value = (event.value + 1 ) / 2
    if state.velocity > 0:
        apply_directional_movement(vjoy)

@MFG_Crosswind_V2_Default.axis(1)
def on_right_brake_move(event, vjoy):
    state.right_brake_value = (event.value + 1) / 2
    if state.velocity > 0:
        apply_directional_movement(vjoy)

syslog.info("Rudder Treadmill: Hold-to-Sprint Logic Active")