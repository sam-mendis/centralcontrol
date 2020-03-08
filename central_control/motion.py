from central_control.afms import afms
from central_control.native_motion import native_motion

class motion:
  """
  generic class for handling substrate movement
  """
  motion_engine = None
  
  # these should be overwritten by a motion controller implementation
  substrate_centers = [160, 140, 120, 100, 80, 60, 40, 20]  # mm from home to the centers of A, B, C, D, E, F, G, H substrates
  photodiode_location = 180  # mm  

  def __init__(self, address='', pcb_object = None):
    """
    sets up communication to motion controller
    """
    if address.startswith('afms'):  # adafruit motor shield
      self.motion_engine = afms(address=address)
      self.substrate_centers = self.motion_engine.substrate_centers
      self.photodiode_location = self.motion_engine.photodiode_location
    elif address.startswith('us'):  # uStepperS via i2c via ethernet connected pcb
      self.motion_engine = native_motion(address=address)
      self.substrate_centers = self.motion_engine.substrate_centers
      self.photodiode_location = self.motion_engine.photodiode_location


      
  def connect(self):
    """
    makes connection to motion controller, might home, blocking
    """
    return self.motion_engine.connect()
    
  def move(self, mm):
    """
    moves mm mm direction, blocking, returns 0 on successful movement
    """
    return self.motion_engine.move(mm)
    
  def goto(self, step_value):
    """
    goes to an absolute mm position, blocking, reuturns 0 on success
    """
    return self.motion_engine.goto(step_value)
    
  def home(self, direction):
    """
    homes to a limit switch, blocking, reuturns 0 on success
    """
    return self.motion_engine.home()
