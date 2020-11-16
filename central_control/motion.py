from central_control.afms import afms
from central_control.us import us
import json

class motion:
  """
  generic class for handling substrate movement
  """
  motion_engine = None

  def __init__(self, address='', pcb_object = None):
    """
    sets up communication to motion controller
    """
    if address.startswith('afms://'):  # adafruit motor shield
      self.motion_engine = afms(address=address)
      self.substrate_centers = self.motion_engine.substrate_centers
      self.photodiode_location = self.motion_engine.photodiode_location
    elif address.startswith('us://'):  # uStepperS via i2c via ethernet connected pcb
      content = address.lstrip('us://')
      pieces = content.split('/')
      expected_lengths_in_mm = pieces[0]
      steps_per_mm = float(pieces[1])

      expected_lengths_in_mm = expected_lengths_in_mm.split(',')
      expected_lengths_in_mm = [float(x) for x in expected_lengths_in_mm]
      steps_per_mm = round(steps_per_mm)

      extra = ''
      keepout = [[]]*len(expected_lengths_in_mm)
      if len(pieces) >= 3:
        try:
          keepout = json.loads(pieces[2])
        except:
          keepout = [[]]*len(expected_lengths_in_mm)
          extra = pieces[2]
        if len(pieces) == 4:
          extra = pieces[3]
      
      # so the format is now
      # driver://csv list of expected lengths in mm/steps per mm/json formatted list of lists of max and min keepout zones/extra
      # for example:
      # us://875,375/6400/[[],[]]

      self.motion_engine = us(pcb_object, expected_lengths=expected_lengths_in_mm, keepout_zones=keepout, steps_per_mm=steps_per_mm, extra=extra)

  def connect(self):
    """
    makes connection to motion controller, blocking
    """
    return self.motion_engine.connect()

  def move(self, mm):
    """
    moves mm mm direction, blocking, returns 0 on successful movement
    """
    return self.motion_engine.move(mm)

  def goto(self, pos):
    """
    goes to an absolute mm position, blocking, reuturns 0 on success
    """
    return self.motion_engine.goto(pos)

  def home(self):
    """
    homes to a limit switch, blocking, reuturns 0 on success
    """
    return self.motion_engine.home()

  def estop(self):
    """
    emergency stop of the driver
    """
    return self.motion_engine.estop()

  def get_position(self):
    """
    returns the current stage location in mm
    """
    return self.motion_engine.get_position()