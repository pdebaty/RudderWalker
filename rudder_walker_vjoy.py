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
sprint_enabled = BoolVariable("Sprint Feature Enabled", "Enable/Disable sprint button functionality", True)
run_threshold = FloatVariable("Run Threshold", "Velocity to hold sprint button", 0.7, 0.1, 0.95)
run_duration = FloatVariable("Run Duration", "Time above threshold to trigger (seconds)", 0.2, 0.1, 1.0)
run_button = IntegerVariable("Run Button", "vJoy button to hold for sprint", 1, 1, 32)

# Toe brake behavior settings
TOE_BRAKE_MODE_CROUCH = 0
TOE_BRAKE_MODE_BACKWARD = 1
toe_brake_mode = IntegerVariable("Toe Brake Mode", "0=Crouch Toggle, 1=Backward Movement", TOE_BRAKE_MODE_CROUCH, 0, 1)
crouch_button = IntegerVariable("Crouch Button", "vJoy button to hold for crouch", 2, 1, 32)

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
    
    # Toe brake state
    left_brake_value = 0.0  
    right_brake_value = 0.0
    both_brakes_pressed = False
    
    # Crouch state
    is_crouching = False

state = TreadmillState()

# ============================================================================
# CORE LOGIC UPDATES
# ============================================================================

def update_run_state(vjoy_handle, current_time):
    """Updates the sprint button state based on current velocity (Hold Logic)"""
    try:
        # Skip sprint logic if sprint feature is disabled or if crouching
        if not sprint_enabled.value or state.is_crouching:
            return
            
        # Get lateral movement to check if we should maintain run state
        has_lateral_movement = state.left_brake_value > 0.1 or state.right_brake_value > 0.1
        
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
        # We release if velocity drops significantly below the threshold AND there's no lateral movement
        if state.is_running and state.velocity < run_threshold.value and not has_lateral_movement:
            state.is_running = False
            state.above_threshold_time = None
            vjoy_handle[state.vjoy_id].button(run_button.value).is_pressed = False
            syslog.info(f"Rudder Treadmill: SPRINT HOLD OFF (velocity: {state.velocity:.2f})")
            
    except Exception as e:
        syslog.error(f"Rudder Treadmill: Error updating run button: {str(e)}")

def toggle_crouch_mode(vjoy_handle):
    """Toggle crouch mode on/off"""
    state.is_crouching = not state.is_crouching
    
    try:
        # Update crouch button state
        vjoy_handle[state.vjoy_id].button(crouch_button.value).is_pressed = state.is_crouching
        
        # If we're crouching, make sure sprint is off
        if state.is_crouching and state.is_running:
            state.is_running = False
            vjoy_handle[state.vjoy_id].button(run_button.value).is_pressed = False
        
        syslog.info(f"Rudder Treadmill: CROUCH {'ON' if state.is_crouching else 'OFF'}")
    except Exception as e:
        syslog.error(f"Rudder Treadmill: Error toggling crouch mode: {str(e)}")

def apply_forward_movement(vjoy_handle):
    """Calculate forward movement value based on velocity and toe brake mode"""
    if state.velocity <= 0.01:
        try:
            vjoy_handle[state.vjoy_id].axis(vjoy_forward_axis.value).value = 0.0
        except Exception as e:
            syslog.error(f"Rudder Treadmill: Error zeroing forward axis: {str(e)}")
        return 0.0
    
    forward_value = state.velocity

    if state.both_brakes_pressed and toe_brake_mode.value == TOE_BRAKE_MODE_BACKWARD:
        forward_value = -forward_value
    
    try:
        vjoy_handle[state.vjoy_id].axis(vjoy_forward_axis.value).value = forward_value
    except Exception as e:
        syslog.error(f"Rudder Treadmill: Error updating forward axis: {str(e)}")
    
    return forward_value

def check_both_brakes_state(vjoy_handle):
    """Check if both brakes are pressed/released and handle accordingly"""
    both_pressed = state.left_brake_value > 0.1 and state.right_brake_value > 0.1
    
    # If both brakes just got pressed, record the state
    if both_pressed and not state.both_brakes_pressed:
        state.both_brakes_pressed = True
    
    # If both brakes were pressed but now released, toggle crouch in crouch mode
    elif not both_pressed and state.both_brakes_pressed:
        state.both_brakes_pressed = False
        if toe_brake_mode.value == TOE_BRAKE_MODE_CROUCH:
            toggle_crouch_mode(vjoy_handle)

def decay_loop(vjoy_handle):
    syslog.info("Rudder Treadmill: Decay thread started")
    
    while state.decay_thread_running and state.velocity > 0:
        current_time = time.time()
        state.velocity *= decay_rate.value
        if state.velocity < 0.01:
            state.velocity = 0.0
        
        apply_forward_movement(vjoy_handle)
        update_run_state(vjoy_handle, current_time)
        time.sleep(0.02)
    
    try:
        vjoy_handle[state.vjoy_id].axis(vjoy_forward_axis.value).value = 0.0
        # Don't reset lateral axis here as it's controlled directly by toe brakes
        
        # Ensure button is released when stopping
        if sprint_enabled.value and state.is_running and not state.is_crouching:
            # Only release if there's no lateral movement
            has_lateral_movement = state.left_brake_value > 0.1 or state.right_brake_value > 0.1
            if not has_lateral_movement:
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
    
    apply_forward_movement(vjoy)
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
    # Normalize from -1.0 to 1.0 range to 0.0 to 1.0 range
    state.left_brake_value = (event.value + 1) / 2
    # Check if both brakes state changed
    check_both_brakes_state(vjoy)
    if state.both_brakes_pressed:
        return
    vjoy[state.vjoy_id].axis(vjoy_lateral_axis.value).value = -state.left_brake_value
      
    # Update run state if there's lateral movement
    if state.velocity > 0:
        update_run_state(vjoy, time.time())

@MFG_Crosswind_V2_Default.axis(1)
def on_right_brake_move(event, vjoy):
    # Normalize from -1.0 to 1.0 range to 0.0 to 1.0 range
    state.right_brake_value = (event.value + 1) / 2
    # Check if both brakes state changed
    check_both_brakes_state(vjoy)
    if state.both_brakes_pressed:
        return
    vjoy[state.vjoy_id].axis(vjoy_lateral_axis.value).value = state.right_brake_value
    
    # Update run state if there's lateral movement
    if state.velocity > 0:
        update_run_state(vjoy, time.time())

syslog.info("Rudder Treadmill: Hold-to-Sprint Logic Active")
