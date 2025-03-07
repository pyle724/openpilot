import os
from common.params import Params
from common.basedir import BASEDIR
from selfdrive.version import get_comma_remote, get_tested_branch
from selfdrive.car.fingerprints import eliminate_incompatible_cars, all_legacy_fingerprint_cars
from selfdrive.car.vin import get_vin, VIN_UNKNOWN
from selfdrive.car.fw_versions import get_fw_versions, match_fw_to_car
from selfdrive.swaglog import cloudlog
import cereal.messaging as messaging
from selfdrive.car import gen_empty_fingerprint

from cereal import car
EventName = car.CarEvent.EventName


def get_startup_event(car_recognized, controller_available, fw_seen):
  if get_comma_remote() and get_tested_branch():
    event = EventName.startup
  else:
    event = EventName.startupMaster

  if not car_recognized:
    if fw_seen:
      event = EventName.startupNoCar
    else:
      event = EventName.startupNoFw
  elif car_recognized and not controller_available:
    event = EventName.startupNoControl
  return event


def get_one_can(logcan):
  while True:
    can = messaging.recv_one_retry(logcan)
    if len(can.can) > 0:
      return can


def load_interfaces(brand_names):
  ret = {}
  for brand_name in brand_names:
    path = ('selfdrive.car.%s' % brand_name)
    CarInterface = __import__(path + '.interface', fromlist=['CarInterface']).CarInterface

    if os.path.exists(BASEDIR + '/' + path.replace('.', '/') + '/carstate.py'):
      CarState = __import__(path + '.carstate', fromlist=['CarState']).CarState
    else:
      CarState = None

    if os.path.exists(BASEDIR + '/' + path.replace('.', '/') + '/carcontroller.py'):
      CarController = __import__(path + '.carcontroller', fromlist=['CarController']).CarController
    else:
      CarController = None

    for model_name in brand_names[brand_name]:
      ret[model_name] = (CarInterface, CarController, CarState)
  return ret


def _get_interface_names():
  # read all the folders in selfdrive/car and return a dict where:
  # - keys are all the car names that which we have an interface for
  # - values are lists of spefic car models for a given car
  brand_names = {}
  for car_folder in [x[0] for x in os.walk(BASEDIR + '/selfdrive/car')]:
    try:
      brand_name = car_folder.split('/')[-1]
      model_names = __import__('selfdrive.car.%s.values' % brand_name, fromlist=['CAR']).CAR
      model_names = [getattr(model_names, c) for c in model_names.__dict__.keys() if not c.startswith("__")]
      brand_names[brand_name] = model_names
    except (ImportError, IOError):
      pass

  return brand_names


# imports from directory selfdrive/car/<name>/
interface_names = _get_interface_names()
interfaces = load_interfaces(interface_names)


# **** for use live only ****
def fingerprint(logcan, sendcan):
  fixed_fingerprint = os.environ.get('FINGERPRINT', "")
  skip_fw_query = os.environ.get('SKIP_FW_QUERY', False)

  if not fixed_fingerprint and not skip_fw_query:
    # Vin query only reliably works thorugh OBDII
    bus = 1

    eps_found = False
    tries = 0
    car_fw = None

    cached_params = Params().get("CarParamsCache")
    if cached_params is not None:
      cached_params = car.CarParams.from_bytes(cached_params)
      if cached_params.carName == "mock":
        cached_params = None

    if cached_params is not None and len(cached_params.carFw) > 0:
      cloudlog.warning("Using cached CarParams")
								
      car_fw = list(cached_params.carFw)
    else:
      cloudlog.warning("Getting VIN & FW versions")
											
      car_fw = get_fw_versions(logcan, sendcan, bus)
    
    print("Printing Car FWs: First Try:")
    print(car_fw)

    for fw in car_fw:
      if fw.ecu == "eps":
        eps_found = True

    while not eps_found and tries < 6:
      print("EPS NOT DETECTED. RETRYING FW FINGERPRINT.")
      car_fw = get_fw_versions(logcan, sendcan, bus)
      for fw in car_fw:
        if fw.ecu == "eps":
          eps_found = True
      tries += 1
    
    if not eps_found:
      print("EPS FW NOT FOUND. THIS IS BAD.")

    exact_fw_match, fw_candidates = match_fw_to_car(car_fw)
  else:
					 
    exact_fw_match, fw_candidates, car_fw = True, set(), []

  vin = VIN_UNKNOWN #Got rid of VIN collection because it's useless. -wirelessnet2

  cloudlog.warning("VIN %s", vin)
  Params().put("CarVin", vin)

  finger = gen_empty_fingerprint()
  candidate_cars = {i: all_legacy_fingerprint_cars() for i in [0, 1]}  # attempt fingerprint on both bus 0 and 1
  frame = 0
  frame_fingerprint = 10  # 0.1s
  car_fingerprint = None
  done = False

  while not done:
    a = get_one_can(logcan)

    for can in a.can:
      # The fingerprint dict is generated for all buses, this way the car interface
      # can use it to detect a (valid) multipanda setup and initialize accordingly
      if can.src < 128:
        if can.src not in finger.keys():
          finger[can.src] = {}
        finger[can.src][can.address] = len(can.dat)

      for b in candidate_cars:
        # Ignore extended messages and VIN query response.
        if can.src == b and can.address < 0x800 and can.address not in [0x7df, 0x7e0, 0x7e8]:
          candidate_cars[b] = eliminate_incompatible_cars(can, candidate_cars[b])

    # if we only have one car choice and the time since we got our first
    # message has elapsed, exit
    for b in candidate_cars:
      if len(candidate_cars[b]) == 1 and frame > frame_fingerprint:
        # fingerprint done
        car_fingerprint = candidate_cars[b][0]

    # bail if no cars left or we've been waiting for more than 2s
    failed = (all(len(cc) == 0 for cc in candidate_cars.values()) and frame > frame_fingerprint) or frame > 200
    succeeded = car_fingerprint is not None
    done = failed or succeeded

    frame += 1

  exact_match = True
  source = car.CarParams.FingerprintSource.can

  # If FW query returns exactly 1 candidate, use it
  if len(fw_candidates) == 1:
    car_fingerprint = list(fw_candidates)[0]
    source = car.CarParams.FingerprintSource.fw
    exact_match = exact_fw_match

  if fixed_fingerprint:
    car_fingerprint = fixed_fingerprint
    source = car.CarParams.FingerprintSource.fixed

  cloudlog.event("fingerprinted", car_fingerprint=car_fingerprint,
                 source=source, fuzzy=not exact_match, fw_count=len(car_fw))
  return car_fingerprint, finger, vin, car_fw, source, exact_match


def get_car(logcan, sendcan):
  candidate, fingerprints, vin, car_fw, source, exact_match = fingerprint(logcan, sendcan)

  if candidate is None:
    cloudlog.warning("car doesn't match any fingerprints: %r", fingerprints)
    candidate = "mock"

  CarInterface, CarController, CarState = interfaces[candidate]
  car_params = CarInterface.get_params(candidate, fingerprints, car_fw)
  car_params.carVin = vin
  car_params.carFw = car_fw
  car_params.fingerprintSource = source
  car_params.fuzzyFingerprint = not exact_match

  return CarInterface(car_params, CarController, CarState), car_params
