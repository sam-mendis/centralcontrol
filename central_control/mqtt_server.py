"""MQTT Server for interacting with the system."""

import argparse
import collections
import itertools
import json
import multiprocessing
import os
import sys
import time
import types
import uuid
import warnings

import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish
import numpy as np
import yaml

from mqtt_tools.queue_publisher import MQTTQueuePublisher
import central_control.fabric


def get_args():
    """Get arguments parsed from the command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mqtthost",
        default="127.0.0.1",
        help="IP address or hostname of MQTT broker.",
    )
    return parser.parse_args()


def yaml_include(loader, node):
    """Load tagged yaml files into root file."""
    with open(node.value) as f:
        return yaml.load(f, Loader=yaml.FullLoader)


# bind include function to !include tags in yaml config file
yaml.add_constructor("!include", yaml_include)


def load_config_from_file():
    """Load the configuration file into memory."""
    # try to load the configuration file from the current working directory
    with open("measurement_config.yaml", "r") as f:
        config = yaml.load(f, Loader=yaml.FullLoader)


def start_process(target, args):
    """Start a new process to perform an action if no process is running.

    Parameters
    ----------
    target : function handle
        Function to run in child process.
    args : tuple
        Arguments required by the function.
    """
    global process

    if process.is_alive() is False:
        process = multiprocessing.Process(target=target, args=args)
        process.start()
    else:
        payload = {"level": "warning", "msg": "Measurement server busy!"}
        publish.single("log", json.dumps(payload), qos=2, hostname=cli_args.MQTTHOST)


def stop_process():
    """Stop a running process."""
    global process

    if process.is_alive() is True:
        process.terminate()
    else:
        payload = {
            "level": "warning",
            "msg": "Nothing to stop. Measurement server is idle.",
        }
        publish.single("log", json.dumps(payload), qos=2, hostname=cli_args.MQTTHOST)


def get_timestamp():
    """Create a human readable formatted timestamp string.

    Returns
    -------
    timestamp : str
        Formatted timestamp.
    """
    return time.strftime("[%Y-%m-%d]_[%H-%M-%S_%z]")


def _calibrate_eqe(mqttc, request):
    """Measure the EQE reference photodiode.

    Parameters
    ----------
    mqttc : MQTTQueuePublisher object
        MQTT queue publisher.
    request : dict
        Request dictionary sent to the server.
    """
    _log("Calibrating EQE...", "info", **{"mqttc": mqttc})

    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    args = request["args"]

    # get pixel queue
    if int(args["eqe_pixel_address"], 16) > 0:
        # if the bitmask isn't empty
        try:
            pixel_queue = _build_q(args, experiment="eqe")
        except ValueError as e:
            # there was a problem with the labels and/or layouts list
            _log("CALIBRATION ABORTED! " + str(e), "error", **{"mqttc": mqttc})
            return
    else:
        # if it's emptpy, assume cal diode is connected externally
        pixel_dict = {
            "label": args["labels"][0],
            "layout": None,
            "array_loc": None,
            "pixel": 0,
            "position": None,
            "area": None,
        }
        pixel_queue = collections.deque(pixel_dict)

    _eqe(pixel_queue, request, mqttc, calibration=True)

    _log("EQE calibration complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _calibrate_psu(mqttc, request):
    """Measure the reference photodiode as a funtcion of LED current.

    Parameters
    ----------
    mqttc : MQTTQueuePublisher object
        MQTT queue publisher.
    request : dict
        Request dictionary sent to the server.
    """
    _log("Calibration LED PSU...", "info", **{"mqttc": mqttc})

    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    config = request["config"]
    args = request["args"]
    channel = request["args"]["psu_channel"]

    # get pixel queue
    if int(args["eqe_pixel_address"], 16) > 0:
        # if the bitmask isn't empty
        try:
            pixel_queue = _build_q(args, experiment="eqe")
        except ValueError as e:
            # there was a problem with the labels and/or layouts list
            _log("CALIBRATION ABORTED! " + str(e), "error", **{"mqttc": mqttc})
            return
    else:
        # if it's emptpy, assume cal diode is connected externally
        pixel_dict = {
            "label": args["labels"][0],
            "layout": None,
            "array_loc": None,
            "pixel": 0,
            "position": None,
            "area": None,
        }
        pixel_queue = collections.deque(pixel_dict)

    # connect instruments
    measurement.connect_instruments(
        dummy=False,
        visa_lib=config["visa"]["visa-lib"],
        smu_address=config["smu"]["address"],
        smu_terminator=config["smu"]["terminator"],
        smu_baud=config["smu"]["baud"],
        smu_front_terminals=config["smu"]["front_terminals"],
        smu_two_wire=config["smu"]["two_wire"],
        controller_address=config["controller"]["address"],
        psu_address=config["psu"]["address"],
        psu_terminator=config["psu"]["terminator"],
        psu_baud=config["psu"]["baud"],
    )

    # using smu to measure the current from the photodiode
    measurement.controller.set_relay("iv")

    while len(pixel_queue) > 0:
        pixel = pixel_queue.popleft()
        label = pixel["label"]
        pix = pixel["pixel"]
        _log(
            f"\nOperating on substrate {label}, pixel {pix}...",
            "info",
            **{"mqttc": mqttc},
        )

        # add id str to handlers to display on plots
        idn = f"{label}_pixel{pix}"

        # we have a new substrate
        if last_label != label:
            _log(
                f"New substrate using '{pixel['layout']}' layout!",
                "info",
                **{"mqttc": mqttc},
            )
            last_label = label

        # move to pixel
        measurement.pixel_setup(
            pixel, handler=_handle_stage_data, handler_kwargs={"mqttc": mqttc}
        )

        timestamp = get_timestamp()

        # perform measurement
        psu_calibration = measurement.calibrate_psu(
            channel,
            config["psu"]["calibration"]["max_current"],
            config["psu"]["calibration"]["current_step"],
        )

        # update eqe diode calibration data in atomic thread-safe way
        diode_dict = {"data": psu_calibration, "timestamp": timestamp, "diode": idn}
        mqttc.append_payload(
            f"calibration/psu/channel_{channel}", json.dumps(diode_dict)
        )

    # disconnect instruments
    measurement.sm.disconnect()
    measurement.psu.disconnect()
    measurement.controller.disconnect()

    _log("LED PSU calibration complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _calibrate_spectrum(mqttc, request):
    """Measure the solar simulator spectrum using it's internal spectrometer.

    Parameters
    ----------
    mqttc : MQTTQueuePublisher object
        MQTT queue publisher.
    request : dict
        Request dictionary sent to the server.
    """
    _log("Calibrating solar simulator spectrum...", "info", **{"mqttc": mqttc})

    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    config = request["config"]

    timestamp = get_timestamp()

    measurement.connect_instruments(
        dummy=False,
        visa_lib=config["visa"]["visa-lib"],
        light_address=config["solarsim"]["address"],
    )

    spectrum = measurement.measure_spectrum()

    measurement.le.disconnect()

    # update spectrum  calibration data in atomic thread-safe way
    spectrum_dict = {"data": spectrum, "timestamp": timestamp}

    # publish calibration
    mqttc.append_payload("calibration/spectrum", json.dumps(spectrum_dict))

    _log("Finished calibrating solar simulator spectrum!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _calibrate_solarsim_diodes(request, mqtthost):
    """Calibrate the solar simulator using photodiodes.

    Parameters
    ----------
    mqttc : MQTTQueuePublisher object
        MQTT queue publisher.
    request : dict
        Request dictionary sent to the server.
    """
    _log("Calibrating solar simulator diodes...", "info", **{"mqttc": mqttc})

    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    args = request["args"]

    # get pixel queue
    if int(args["iv_pixel_address"], 16) > 0:
        # if the bitmask isn't empty
        try:
            pixel_queue = _build_q(args, experiment="eqe")
        except ValueError as e:
            # there was a problem with the labels and/or layouts list
            _log("CALIBRATION ABORTED! " + str(e), "error", **{"mqttc": mqttc})
            return
    else:
        # if it's emptpy, assume cal diode is connected externally
        pixel_dict = {
            "label": args["labels"][0],
            "layout": None,
            "array_loc": None,
            "pixel": 0,
            "position": None,
            "area": None,
        }
        pixel_queue = collections.deque(pixel_dict)

    try:
        _ivt(mqttc, request, pixel_queue, calibration=True)
    except ValueError as e:
        _log("CALIBRATION ABORTED! " + str(e), "error", **{"mqttc": mqttc})
        return

    _log("Solar simulator diode calibration complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _calibrate_rtd(request, mqtthost):
    """Calibrate RTD's for temperature measurement.
    
    Parameters
    ----------
    mqttc : MQTTQueuePublisher object
        MQTT queue publisher.
    request : dict
        Request dictionary sent to the server.
    """
    _log("Calibrating RTDs...", "info", **{"mqttc": mqttc})

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # get pixel queue
    if int(args["iv_pixel_address"], 16) > 0:
        # if the bitmask isn't empty
        try:
            pixel_queue = _build_q(args, experiment="eqe")
        except ValueError as e:
            # there was a problem with the labels and/or layouts list
            _log("CALIBRATION ABORTED! " + str(e), "error", **{"mqttc": mqttc})
            return
    else:
        # if it's emptpy, report error
        _log("CALIBRATION ABORTED! No devices selected.", "error", **{"mqttc": mqttc})

    try:
        _ivt(mqttc, request, pixel_queue, calibration=True, rtd=True)
    except ValueError as e:
        _log("CALIBRATION ABORTED! " + str(e), "error", **{"mqttc": mqttc})
        return

    _log("RTD calibration complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _home(request, mqtthost):
    """Home the stage.

    Parameters
    ----------
    mqttc : MQTTQueuePublisher object
        MQTT queue publisher.
    request : dict
        Request dictionary sent to the server.
    """
    _log("Homing stage...", "info", **{"mqttc": mqttc})

    config = request["config"]

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(mqtthost)

    # create fabric measurement logic object and connect instruments
    measurement = central_control.fabric.fabric()
    measurement.connect_instruments(
        dummy=False, controller_address=config["controller"]["address"],
    )

    homing_dict = measurement.home_stage(config["stage"]["length"])
    measurement.controller.disconnect()

    if homing_dict["code"] < 0:
        # homing failed
        _log(homing_dict["msg"], "error", **{"mqttc": mqttc})
    else:
        # homing succeeded
        _log(homing_dict["msg"], "info", **{"mqttc": mqttc})

    _log("Homing complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _goto(request, mqtthost):
    """Go to a stage position.

    Parameters
    ----------
    mqttc : MQTTQueuePublisher object
        MQTT queue publisher.
    request : dict
        Request dictionary sent to the server.
    """
    position = request["args"]["goto"]
    _log(f"Moving to stage position {}...", "info", **{"mqttc": mqttc})

    config = request["config"]

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(mqtthost)

    # create fabric measurement logic object and connect instruments
    measurement = central_control.fabric.fabric()
    measurement.connect_instruments(
        dummy=False, controller_address=config["controller"]["address"],
    )

    goto_dict = measurement.goto_stage_position(
        position, handler=_handle_stage_data, handler_kwargs={"mqttc": mqttc},
    )
    measurement.controller.disconnect()

    if goto_dict["code"] < 0:
        # homing failed
        _log(goto_dict["msg"], "error", **{"mqttc": mqttc})
    else:
        # homing succeeded
        _log(goto_dict["msg"], "info", **{"mqttc": mqttc})

    _log("Goto complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _read_stage(request, mqtthost):
    """Read the stage position."""
    # TODO: complete args for func
    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    measurement.read_stage_position()

    _log("Read stage position complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _contact_check(request, mqtthost):
    """Perform contact check."""
    # TODO: write back to gui

    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    config = request["config"]

    array = config["substrates"]["number"]
    rows = array[0]
    try:
        cols = array[1]
    except IndexError:
        cols = 1
    active_layout = config["substrates"]["active_layout"]
    pcb_adapter = config[active_layout]["pcb_name"]
    pixels = config[pcb_adapter]["pixels"]
    measurement.check_all_contacts(rows, cols, pixels)

    _log("Contact check complete!", "info", **{"mqttc": mqttc})

    mqttc.stop()


def _get_substrate_positions(config, experiment):
    """Calculate absolute positions of all substrate centres.

    Read in info from config file.

    Parameters
    ----------
    experiment : str
        Name used to look up the experiment centre stage position from the config
        file.

    Returns
    -------
    substrate_centres : list of lists
        Absolute substrate centre co-ordinates. Each sublist contains the positions
        along each axis.
    """
    experiment_centre = config["experiment"][experiment]["positions"]

    # read in number substrates in the array along each axis
    substrate_number = config["substrates"]["number"]

    # get number of substrate centres between the centre and the edge of the
    # substrate array along each axis, e.g. if there are 4 rows, there are 1.5
    # substrate centres to the outermost substrate
    substrate_offsets = []
    substrate_total = 1
    for number in substrate_number:
        if number % 2 == 0:
            offset = number / 2 - 0.5
        else:
            offset = np.floor(number / 2)
        substrate_offsets.append(offset)
        substrate_total = substrate_total * number

    # read in substrate spacing in mm along each axis into a list
    substrate_spacing = config["substrates"]["spacing"]

    # read in step length in steps/mm
    steplength = config["stage"]["steplength"]

    # get absolute substrate centres along each axis
    axis_pos = []
    for offset, spacing, number, centre in zip(
        substrate_offsets, substrate_spacing, substrate_number, experiment_centre,
    ):
        abs_offset = offset * (spacing / steplength) + centre
        axis_pos.append(np.linspace(-abs_offset, abs_offset, number))

    # create array of positions
    substrate_centres = list(itertools.product(*axis_pos))

    return substrate_centres


def _get_substrate_index(array_loc, array_size):
    """Get the index of a substrate in a flattened array.

    Parameters
    ----------
    array_loc : list of int
        Position of the substrate in the array along each available axis.
    array_size : list of int
        Number of substrates in the array along each available axis.

    Returns
    -------
    index : int
        Index of the substrate in the flattened array.
    """
    if len(array_loc) > 1:
        # get position along last axis
        last_axis_loc = array_loc.pop()

        # pop length of last axis, it's not needed anymore
        array_size.pop()

        # get the total number of substrates in each subarray comprised of remaining
        # axes
        subarray_total = 1
        for n in array_size:
            subarray_total = subarray_total * n

        # get the number of substrates in all subarrays along the last axis up to the
        # level below the substrate location
        subarray_total = subarray_total * (last_axis_loc - 1)

        # recursively iterate through axes, adding smaller subarray totals as axes are
        # reduced to 1
        index = _get_substrate_index(array_loc, array_size) + subarray_total

    return index


def _build_q(request, experiment):
    """Generate a queue of pixels we'll run through.

    Parameters
    ----------
    args : types.SimpleNamespace
        Experiment arguments.
    experiment : str
        Name used to look up the experiment centre stage position from the config
        file.

    Returns
    -------
    pixel_q : deque
        Queue of pixels to measure.
    """
    # TODO: return support for inferring layout from pcb adapter resistors

    config = request["config"]
    args = request["args"]

    # get substrate centres
    substrate_centres = _get_substrate_positions(config, experiment)
    substrate_total = len(substrate_centres)

    # number of substrates along each available axis
    substrate_number = config["substrates"]["number"]

    # make sure as many layouts as labels were given
    if ((l1 := len(args["layouts"])) != substrate_total) or (
        (l2 := len(args["labels"])) != substrate_total
    ):
        raise ValueError(
            "Lists of layouts and labels must match number of substrates in the "
            + f"array: {substrate_total}. Layouts list has length {l1} and labels list "
            + f"has length {l2}."
        )

    # create a substrate queue where each element is a dictionary of info about the
    # layout from the config file
    substrate_q = []
    i = 0
    for layout, label, centre in zip(args["layouts"], args["labels"], substrate_centres):
        # get pcb adapter info from config file
        pcb_name = config["substrates"]["layouts"][layout]["pcb_name"]

        # read in pixel positions from layout in config file
        config_pos = config["substrates"]["layouts"][layout]["positions"]
        pixel_positions = []
        for pos in range(len(config_pos)):
            abs_pixel_position = [int(x) for x in zip(pos, centre)]
            pixel_positions.append(abs_pixel_position)

        # find co-ordinate of substrate in the array
        _substrates = np.linspace(1, substrate_total, substrate_total)
        _array = np.reshape(_substrates, substrate_number)
        array_loc = [int(ix) + 1 for ix in np.where(_array == i)]

        substrate_dict = {
            "label": label,
            "array_loc": array_loc,
            "layout": layout,
            "pcb_name": pcb_name,
            "pcb_contact_pads": config[pcb_name]["pcb_contact_pads"],
            "pcb_resistor": config[pcb_name]["pcb_resistor"],
            "pixels": config[layout]["pixels"],
            "pixel_positions": pixel_positions,
            "areas": config[layout]["areas"],
        }
        substrate_q.append(substrate_dict)

        i += 1

    # TODO: return support for pixel strings that aren't hex bitmasks
    # convert hex bitmask string into bit list where 1's and 0's represent whether
    # a pixel should be measured or not, respectively
    if experiment == "solarsim":
        pixel_address_string = args["iv_pixel_address"]
    elif experiment == "eqe":
        pixel_address_string = args["eqe_pixel_address"]

    bitmask = [int(x) for x in bin(int(pixel_address_string, 16))[2:]]

    # build pixel queue
    pixel_q = collections.deque()
    for substrate in substrate_q:
        # git bitmask for the substrate pcb
        sub_bitmask = [
            bitmask.pop(-1) for i in range(substrate["pcb_contact_pads"])
        ].reverse()
        # select pixels to measure from layout
        for pixel in substrate["pixels"]:
            if sub_bitmask[pixel - 1] == 1:
                pixel_dict = {
                    "label": substrate["label"],
                    "layout": substrate["layout"],
                    "array_loc": substrate["array_loc"],
                    "pixel": pixel,
                    "position": substrate["pixel_positions"][pixel - 1],
                    "area": substrate["areas"][pixel - 1],
                }
                pixel_q.append(pixel_dict)

    return pixel_q


def _handle_measurement_data(data, **kwargs):
    """Publish measurement data.

    Parameters
    ----------
    data : list
        List of data to publish.
    **kwargs : dict
        Dictionary of additional keyword arguments required by handler.
    """
    kind = kwargs["kind"]
    idn = kwargs["idn"]
    mqttc = kwargs["mqttc"]

    payload = {"data": data, "id": idn, "clear": False, "end": False}
    mqttc.append_payload(f"data/raw/{kind}", json.dumps(payload))


def _handle_stage_data(data, **kwargs):
    """Publish stage position data.

    Parameters
    ----------
    data : list
        List of data to publish.
    **kwargs : dict
        Dictionary of additional keyword arguments required by handler.
    """
    mqttc = kwargs["mqttc"]

    mqttc.append_payload("stage_position", json.dumps(data))


def _handle_contact_check(pixel_msg, **kwargs):
    """Publish stage position data.

    Parameters
    ----------
    settings : dict
        Dictionary of save settings.
    **kwargs : dict
        Dictionary of additional keyword arguments required by handler.
    """
    mqttc = kwargs["mqttc"]

    mqttc.append_payload("contact_check", json.dumps(pixel_msg))


def _log(msg, level, **kwargs):
    """Publish info for logging.

    Parameters
    ----------
    msg : str
        Log message.
    level : str
        Log level.
    **kwargs : dict
        Dictionary of additional keyword arguments required by handler.
    """
    mqttc = kwargs["mqttc"]

    payload = {"level": level, "msg": msg}
    mqttc.append_payload("log", json.dumps(payload))


def _ivt(pixel_queue, request, measurement, mqttc, calibration=False, rtd=False):
    """Run through pixel queue of i-v-t measurements.

    Paramters
    ---------
    pixel_queue : deque of dict
        Queue of dictionaries of pixels to measure.
    request : dict
        Experiment arguments.
    mqttc : MQTTQueuePublisher
        MQTT queue publisher client.
    measurement : measurement logic object
        Object controlling instruments and measurements.
    calibration : bool
        Calibration flag.
    rtd : bool
        RTD flag for type of calibration. Used for reporting.
    """
    config = request["config"]
    args = request["args"]

    # connect instruments
    measurement.connect_instruments(
        dummy=False,
        visa_lib=config["visa"]["visa-lib"],
        smu_address=config["smu"]["address"],
        smu_terminator=config["smu"]["terminator"],
        smu_baud=config["smu"]["baud"],
        smu_front_terminals=config["smu"]["front_terminals"],
        smu_two_wire=config["smu"]["two_wire"],
        controller_address=config["controller"]["address"],
        light_address=config["solarsim"]["address"],
    )

    # set the master experiment relay
    measurement.controller.set_relay("iv")

    last_label = None
    # scan through the pixels and do the requested measurements
    while len(pixel_queue) > 0:
        # instantiate container for all measurement data on pixel
        data = []

        # get pixel info
        pixel = pixel_queue.popleft()
        label = pixel["label"]
        pix = pixel["pixel"]
        _log(
            f"\nOperating on substrate {label}, pixel {pix}...",
            "info",
            **{"mqttc": mqttc},
        )

        # add id str to handlers to display on plots
        idn = f"{label}_pixel{pix}"

        # check if there is have a new substrate
        if last_label != label:
            _log(
                f"New substrate using '{pixel['layout']}' layout!",
                "info",
                **{"mqttc": mqttc},
            )
            last_label = label

        # move to pixel
        measurement.pixel_setup(
            pixel, handler=_handle_stage_data, handler_kwargs={"mqttc": mqttc}
        )

        # init parameters derived from steadystate measurements
        ssvoc = None

        # get or estimate compliance current
        if type(args["current_compliance_override"]) == float:
            compliance_i = args["current_compliance_override"]
        else:
            # estimate compliance current based on area
            compliance_i = measurement.compliance_current_guess(pixel["area"])

        if calibration is False:
            handler = _handle_measurement_data
        else:
            handler = None
            handler_kwargs = {}

        timestamp = get_timestamp()

        # steady state v@constant I measured here - usually Voc
        if args["v_t"] > 0:
            # clear v@constant I plot
            mqttc.append_payload("plot/vt/clear", json.dumps(""))

            if calibration is False:
                handler_kwargs = {"kind": "vt_measurement", "idn": idn, "mqttc": mqttc}

            vt = measurement.steady_state(
                t_dwell=args["v_t"],
                NPLC=args["steadystate_nplc"],
                stepDelay=args["steadystate_step_delay"],
                sourceVoltage=False,
                compliance=args["voltage_compliance_override"],
                senseRange="a",
                setPoint=args["steadystate_i"],
                handler=handler,
                handler_kwargs=handler_kwargs,
            )

            data += vt

            # if this was at Voc, use the last measurement as estimate of Voc
            if args["steadystate_i"] == 0:
                ssvoc = vt[-1]
                measurement.mppt.Voc = ssvoc

        if (args["sweep_1"] is True) or (args["sweep_2"] is True):
            # clear iv plot
            mqttc.append_payload("plot/iv/clear", json.dumps(""))

        # TODO: add support for dark measurement, has to use autorange
        if args["sweep_1"] is True:
            # determine sweep start voltage
            if type(args["scan_start_override_1"]) == float:
                start = args["scan_start_override_1"]
            elif ssvoc is not None:
                start = ssvoc * (1 + (config["iv"]["percent_beyond_voc"] / 100))
            else:
                raise ValueError(
                    f"Start voltage wasn't given and couldn't be inferred."
                )

            # determine sweep end voltage
            if type(args["scan_end_override_1"]) == float:
                end = args["scan_end_override_1"]
            else:
                end = -1 * np.sign(ssvoc) * config["iv"]["voltage_beyond_isc"]

            _log(
                f"Sweeping voltage from {start} V to {end} V",
                "info",
                **{"mqttc": mqttc},
            )

            if calibration is False:
                handler_kwargs = {"kind": "iv_measurement", "idn": idn, "mqttc": mqttc}

            iv1 = measurement.sweep(
                sourceVoltage=True,
                compliance=compliance_i,
                senseRange="f",
                nPoints=args["scan_points"],
                stepDelay=args["scan_step_delay"],
                start=start,
                end=end,
                NPLC=args["scan_nplc"],
                handler=handler,
                handler_kwargs=handler_kwargs,
            )

            data += iv1

            Pmax_sweep1, Vmpp1, Impp1, maxIx1 = measurement.mppt.which_max_power(iv1)

        if args["sweep_2"] is True:
            # sweep the opposite way to sweep 1
            start = end
            end = start

            _log(
                f"Sweeping voltage from {start} V to {end} V",
                "info",
                **{"mqttc": mqttc},
            )

            if calibration is False:
                handler_kwargs = {"kind": "iv_measurement", "idn": idn, "mqttc": mqttc}

            iv2 = measurement.sweep(
                sourceVoltage=True,
                senseRange="f",
                compliance=compliance_i,
                nPoints=args["scan_points"],
                start=start,
                end=end,
                NPLC=args["scan_nplc"],
                handler=handler,
                handler_kwargs=handler_kwargs,
            )

            data += iv2

            Pmax_sweep2, Vmpp2, Impp2, maxIx2 = measurement.mppt.which_max_power(iv2)

        # TODO: read and interpret parameters for smart mode
        # # determine Vmpp and current compliance for mppt
        # if (self.args["sweep_1"] is True) & (self.args["sweep_2"] is True):
        #     if abs(Pmax_sweep1) > abs(Pmax_sweep2):
        #         Vmpp = Vmpp1
        #         compliance_i = Impp1 * 5
        #     else:
        #         Vmpp = Vmpp2
        #         compliance_i = Impp2 * 5
        # elif self.args["sweep_1"] is True:
        #     Vmpp = Vmpp1
        #     compliance_i = Impp1 * 5
        # else:
        #     # no sweeps have been measured so max power tracker will estimate Vmpp
        #     # based on Voc (or measure it if also no Voc) and will use initial
        #     # compliance set before any measurements were taken.
        #     Vmpp = None
        # self.logic.mppt.Vmpp = Vmpp
        measurement.mppt.current_compliance = compliance_i

        if args["mppt_t"] > 0:
            _log(
                f"Tracking maximum power point for {args["mppt_t"]} seconds.",
                "info",
                **{"mqttc": mqttc},
            )

            # clear mppt plot
            mqttc.append_payload("plot/mppt/clear", json.dumps(""))

            if calibration is False:
                handler_kwargs = {
                    "kind": "mppt_measurement",
                    "idn": idn,
                    "mqttc": mqttc,
                }

            # measure voc for 1s to initialise mppt
            vt = measurement.steady_state(
                t_dwell=1,
                NPLC=args["steadystate_nplc"],
                stepDelay=args["steadystate_step_delay"],
                sourceVoltage=False,
                compliance=args["voltage_compliance_override"],
                senseRange="a",
                setPoint=0,
                handler=handler,
                handler_kwargs=handler_kwargs,
            )
            measurement.mppt.Voc = vt[-1]

            mt = measurement.track_max_power(
                args["mppt_t"],
                NPLC=args["steadystate_nplc"],
                stepDelay=args["steadystate_step_delay"],
                extra=args["mppt_params"],
                handler=handler,
                handler_kwargs=handler_kwargs,
            )

            data += vt
            data += mt

        if args["i_t"] > 0:
            # steady state I@constant V measured here - usually Isc
            # clear I@constant V plot
            mqttc.append_payload("plot/it/clear", json.dumps(""))

            if calibration is False:
                handler_kwagrgs = {"kind": "it_measurement", "idn": idn, "mqttc": mqttc}

            it = measurement.steady_state(
                t_dwell=args["i_t"],
                NPLC=args["steadystate_nplc"],
                stepDelay=args["steadystate_step_delay"],
                sourceVoltage=True,
                compliance=compliance_i,
                senseRange="a",
                setPoint=args["steadystate_v"],
                handler=handler,
                handler_kwargs=handler_kwagrgs,
            )

            data += it

        if calibration is True:
            diode_dict = {"data": data, "timestamp": timestamp, "diode": idn}
            if rtd is True:
                mqttc.append_payload("calibration/rtd", json.dumps(diode_dict))
            else:
                mqttc.append_payload("calibration/solarsim_diode", json.dumps(diode_dict))

    measurement.run_done()


def _eqe(pixel_queue, request, mqttc, measurement, calibration=False):
    """Run through pixel queue of EQE measurements.

    Paramters
    ---------
    pixel_queue : deque of dict
        Queue of dictionaries of pixels to measure.
    request : dict
        Experiment arguments.
    mqttc : MQTTQueuePublisher
        MQTT queue publisher client.
    measurement : measurement logic object
        Object controlling instruments and measurements.
    calibration : bool
        Calibration flag.
    """
    config = request["config"]
    args = request["args"]

    # connect instruments
    measurement.connect_instruments(
        dummy=False,
        visa_lib=config["visa"]["visa-lib"],
        smu_address=config["smu"]["address"],
        smu_terminator=config["smu"]["terminator"],
        smu_baud=config["smu"]["baud"],
        smu_front_terminals=config["smu"]["front_terminals"],
        smu_two_wire=config["smu"]["two_wire"],
        controller_address=config["controller"]["address"],
        lia_address=config["lia"]["address"],
        lia_terminator=config["lia"]["terminator"],
        lia_baud=config["lia"]["baud"],
        lia_output_interface=config["lia"]["output_interface"],
        mono_address=config["monochromator"]["address"],
        mono_terminator=config["monochromator"]["terminator"],
        mono_baud=config["monochromator"]["baud"],
    )

    measurement.controller.set_relay("eqe")

    while len(pixel_queue) > 0:
        pixel = pixel_queue.popleft()
        label = pixel["label"]
        pix = pixel["pixel"]
        _log(
            f"\nOperating on substrate {label}, pixel {pix}...",
            "info",
            **{"mqttc": mqttc},
        )

        # add id str to handlers to display on plots
        idn = f"{label}_pixel{pix}"

        # we have a new substrate
        if last_label != label:
            _log(
                f"New substrate using '{pixel['layout']}' layout!",
                "info",
                **{"mqttc": mqttc},
            )
            last_label = label

        # move to pixel
        measurement.pixel_setup(
            pixel, handler=_handle_stage_data, handler_kwargs={"mqttc": mqttc}
        )

        _log(
            f"Scanning EQE from {args["eqe_start_wl"]} nm to {args["eqe_end_wl"]} nm",
            "info",
            **{"mqttc": mqttc},
        )

        # determine how live measurement data will be handled
        if calibration is True:
            handler = None
            handler_kwargs = {}
        else:
            handler = _handle_measurement_data
            handler_kwargs = {"idn": idn, "mqttc": mqttc}

        # clear eqe plot
        mqttc.append_payload("plot/eqe/clear", json.dumps(""))

        # get human-readable timestamp
        timestamp = get_timestamp()

        # perform measurement
        eqe = measurement.eqe(
            psu_ch1_voltage=config["psu"]["ch1_voltage"],
            psu_ch1_current=args["psu_is"][0],
            psu_ch2_voltage=config["psu"]["ch2_voltage"],
            psu_ch2_current=args["psu_is"][1],
            psu_ch3_voltage=config["psu"]["ch3_voltage"],
            psu_ch3_current=args["psu_is"][2],
            smu_voltage=args["eqe_smu_v"],
            start_wl=args["eqe_start_wl"],
            end_wl=args["eqe_end_wl"],
            num_points=args["eqe_num_wls"],
            grating_change_wls=config["monochromator"]["grating_change_wls"],
            filter_change_wls=config["monochromator"]["filter_change_wls"],
            auto_gain=not (args["eqe_autogain_off"]),
            auto_gain_method=args["eqe_autogain_method"],
            integration_time=args["eqe_integration_time"],
            handler=handler,
            handler_kwargs=handler_kwargs,
        )

        # update eqe diode calibration data in
        if calibration is True:
            diode_dict = {"data": eqe, "timestamp": timestamp, "diode": idn}
            mqttc.append_payload(
                "calibration/eqe", json.dumps(diode_dict), retain=True,
            )

    # disconnect instruments
    measurement.sm.disconnect()
    measurement.lia.disconnect()
    measurement.mono.disconnect()
    measurement.controller.disconnect()


def _test_hardware(mqttc, request, config):
    """Test hardware."""
    # TODO: fill in func
    pass


def _run(request, mqtthost):
    """Act on command line instructions.

    Parameters
    ----------
    request : dict
        Dictionary of configuration settings and measurement arguments.
    mqtthost : str
        MQTT broker IP address or host name.
    """
    # create fabric measurement logic object
    measurement = central_control.fabric.fabric()

    # create temporary mqtt client
    mqttc = MQTTQueuePublisher()
    mqttc.run(cli_args.MQTTHOST)

    args = request["args"]

    # build up the queue of pixels to run through
    if args["dummy"] is True:
        args["iv_pixel_address"] = "0x1"
        args["eqe_pixel_address"] = "0x1"

    if args["iv_pixel_address"] is not None:
        try:
            iv_pixel_queue = _build_q(args, experiment="solarsim")
        except ValueError as e:
            # there was a problem with the labels and/or layouts list
            _log("RUN ABORTED! " + str(e), "error", **{"mqttc": mqttc})
            return
    else:
        iv_pixel_queue = []

    if args["eqe_pixel_address"] is not None:
        try:
            eqe_pixel_queue = _build_q(args, experiment="eqe")
        except ValueError as e:
            _log("RUN ABORTED! " + str(e), "error", **{"mqttc": mqttc})
            return
    else:
        eqe_pixel_queue = []

    # measure i-v-t
    if len(iv_pixel_queue) > 0:
        try:
            _ivt(iv_pixel_queue, request, measurement, mqttc)
        except ValueError as e:
            _log("RUN ABORTED! " + str(e), "error", **{"mqttc": mqttc})
            return

    # measure eqe
    if len(eqe_pixel_queue) > 0:
        _eqe(eqe_pixel_queue, request, measurement, mqttc)

    # report complete
    _log("Run complete!", "info", **{"mqttc": mqttc})

    # close mqtt client cleanly
    mqttc.stop()


def on_message(mqttc, obj, msg):
    """Act on an MQTT message.

    Actions that require instrument I/O run in a worker process. Only one action
    process can run at a time. If an action process is running the server will
    report that it's busy.
    """
    request = json.loads(msg.payload)

    # perform a requested action
    if (action := msg.topic.split("/")[-1]) == "run":
        start_process(_run, (request, cli_args.MQTTHOST,))
    elif action == "stop":
        stop_process()
    elif action == "calibrate_eqe":
        start_process(_calibrate_eqe, (request, cli_args.MQTTHOST,))
    elif action == "calibrate_psu":
        start_process(_calibrate_psu, (request, cli_args.MQTTHOST,))
    elif action == "calibrate_solarsim_diodes":
        start_process(_calibrate_solarsim_diodes, (request, cli_args.MQTTHOST,))
    elif action == "calibrate_spectrum":
        start_process(_calibrate_spectrum, (request, cli_args.MQTTHOST,))
    elif action == "calibrate_rtd":
        start_process(_calibrate_rtd, (request, cli_args.MQTTHOST,))
    elif action == "home":
        start_process(_home, (request, cli_args.MQTTHOST,))
    elif action == "goto":
        start_process(_goto, (request, cli_args.MQTTHOST,))
    elif action == "read_stage":
        start_process(_read_stage, (request, cli_args.MQTTHOST,))


# required when using multiprocessing in windows, advised on other platforms
if __name__ == "__main__":
    # get command line arguments
    cli_args = get_args()

    # create dummy process
    process = multiprocessing.Process()

    # create mqtt client id
    client_id = f"measure-{uuid.uuid4().hex}"

    # setup mqtt subscriber client
    mqttc = mqtt.Client(client_id=client_id)
    mqttc.on_message = on_message
    mqttc.connect(cli_args.MQTTHOST)
    mqttc.subscribe("measurement/#", qos=2)
    mqttc.loop_forever()
