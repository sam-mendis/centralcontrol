#!/usr/bin/env python3

from collections import deque
import time


class us:
    """interface to uStepperS via i2c via ethernet connected pcb"""

    # substrate_centers = [300, 260, 220, 180, 140, 100, 60, 20]  # mm from home to the centers of A, B, C, D, E, F, G, H substrates

    motor_steps_per_rev = 200  # steps/rev
    micro_stepping = 256  # microsteps/step
    screw_pitch = 8.0  # mm/rev

    allowed_length_deviation = (
        5  # measured length can deviate from expected length by up to this, in mm
    )
    end_buffers = 5  # don't allow movement to within this many mm of the stage ends (needed to prevent potential homing issues)

    otter_safe_x = 550  # xaxis safe location to home y for otter (in mm)

    def __init__(
        self,
        pcb_object,
        expected_lengths,
        keepout_zones,
        steps_per_mm=motor_steps_per_rev * micro_stepping / screw_pitch,
        extra="",
    ):
        """
    sets up the microstepper object
    needs handle to active PCB class object
    """
        self.pcb = pcb_object
        self.steps_per_mm = steps_per_mm
        self.extra = extra
        self.expected_lengths = (
            expected_lengths  # this gets converted to steps in connect()
        )
        self.homed = None
        self.keepout_zones = keepout_zones

    def __del__(self):
        pass

    def connect(self):
        """
    opens connection to the motor controller
    """
        ret = -1

        try:
            stage_controllers = int(self.pcb.get("e"))
            self.axes = []
            self.current_position = []  # in mm
            user_expected_lengths = self.expected_lengths
            user_n_axes = len(user_expected_lengths)
            self.expected_lengths = []  # in steps
            self.measured_lengths = []  # in steps
            max_axes = 3
            for i in range(max_axes):
                if (stage_controllers >> i) & 1 == 1:
                    self.axes += [i + 1]
                    self.current_position += [None]
                    self.expected_lengths += [None]
                    self.measured_lengths += [None]

            self.n_axes = len(self.axes)
            if self.n_axes != len(user_expected_lengths):
                print(
                    f"Warning: the user gave us {user_n_axes} expected lengths, but we found {self.n_axes} axes"
                )
            else:
                for i, ax in enumerate(self.axes):
                    here = self.pcb.get(f"r{ax}")
                    try:
                        self.current_position[i] = here / self.steps_per_mm
                    except:
                        self.current_position[i] = here
                    self.expected_lengths[i] = (
                        user_expected_lengths[i] * self.steps_per_mm
                    )  # to steps

                if self.check_lengths() != 0:
                    print(f"Warning: stage lengths did not check out")

                ret = 0
        except:
            pass
        return ret

    # check stage lengths
    # axis = -1 check them all
    # return codes
    #  0 ok
    # -1 lengths not okay
    # -2 length(s) unknowable: homing required or currently homing or length request error
    # -3 invalid axis
    # -9 programming error
    def check_lengths(self, axis=-1):
        ret = -9
        if axis == -1:
            to_check = self.axes
        else:
            if axis in self.axes:
                to_check = [axis]
            else:
                to_check = []
                ret = -3
        for ax in to_check:
            driver_length = self.pcb.get(f"l{ax}")
            self.measured_lengths[self.axes.index(ax)] = driver_length
            if (driver_length is not None) and (driver_length > 0):
                ald = self.allowed_length_deviation * self.steps_per_mm
                el = self.expected_lengths[self.axes.index(ax)]
                if (driver_length < el + ald) and (driver_length > el - ald):
                    ret = 0
                else:
                    print(f"{driver_length} is not on ({el-ald},{el+ald})")
                    ret = -1
                    break
            else:
                self.homed = False
                ret = -2
                break
        if axis == -1:
            if ret == 0:
                self.homed = True
            else:
                self.homed = False
        print(f"check_result {ret}")
        return ret

    # homes the whole stage or just one axis
    # axis = -1 homes them all, 1 is the first one
    # block execution during procedure if block = True
    # otter must call with block=True and axis =-1
    # returns:
    # a list of measured axes lengths in mm when block == true and no error
    # 0 when block == false and no error
    # -1 if the homing timed out
    # -2 if a command was not properly acknowledged by the controlbox (possibly already homing?)
    # -3 invalid axis
    # -4 the otter stage can not home one axis alone, nor can it do non-blocking homes
    # -5 otter home failed because stage 1 was not the expected length
    # -9 if there was a programming error
    def home(self, axis=-1, block=True, timeout=130, enable_otter=True):
        ret = -9
        t0 = time.time()
        if ("otter" in self.extra) and (enable_otter == True):
            if (axis == -1) and (block == True):
                time_left = timeout - (time.time() - t0)
                ret = self.otter_home(safex=self.otter_safe_x, timeout=time_left)
            else:
                ret = -4
        else:  # non-otter home
            if axis == -1:
                cmd = "h"
            else:
                cmd = f"h{axis}"
                if axis not in self.axes:
                    ret = -3
            if ret != -3:
                result = self.pcb.get(cmd)
                if result != "":
                    ret = -2
                else:
                    if block == True:
                        time_left = timeout - (time.time() - t0)
                        ret = self.wait_for_home_or_jog(axis=axis, timeout=time_left)
                    else:  # non-blocking home
                        ret = 0
        return ret

    # homes otter's stage
    # safex is the x axis position that is safe to home y like normal
    # this always blocks
    # return codes
    # a list of the stage dimensions if there was no error
    # -1 if the timeout expired before the move completed (for block=True mode)
    # -2 if a command was rejected by the firmware (maybe the axes is unhomed or currently homing?)
    # -3 invalid axis
    # -5 ax1 was not the expected length
    # -6 attempt to move out of bounds
    # -7 location and axes list length mismatch
    # -8 movement concluded, but we did not reach the goal (stall?)
    # -9 for programming error
    def otter_home(self, safex=otter_safe_x, timeout=250):
        ret = -9
        t0 = time.time()
        dims = [0, 0]
        ret = self.jog(2, direction="b", block=True, timeout=timeout)
        if ret == 0:  # ax2 jogged to motor end extreme
            time_left = timeout - (time.time() - t0)
            ret = self.home(axis=1, block=True, timeout=time_left, enable_otter=False)
            if isinstance(ret, list):  # ax1 homed
                dims[0] = ret[0]
                ret = self.check_lengths(1)
                if ret == 0:  # ax1 is the expected length
                    time_left = timeout - (time.time() - t0)
                    ret = self.goto(safex, axes=1, block=True, timeout=time_left)
                    if ret == 0:  # ax1 safe loaction reached
                        time_left = timeout - (time.time() - t0)
                        ret = self.home(
                            axis=2, block=True, timeout=time_left, enable_otter=False
                        )
                        if isinstance(ret, list):  # ax2 homed
                            dims[1] = ret[0]
                            ret = dims
                            self.check_lengths()
                else:
                    ret = -5
        return ret

    # jogs an axis in either direction 'a' or 'b'
    # block == true means execution will not continue unil the motor stalls or the timeout expires
    # returns:
    # 0 for success
    # -1 if timeout while waiting for completion
    # -2 if a command was not properly acknowledged by the controlbox
    # -3 invalid axis
    # -9 for programming error
    def jog(self, axis, direction="b", block=True, timeout=80):
        ret = -9
        t0 = time.time()
        if axis not in self.axes:
            ret = -3
        else:
            result = self.pcb.get(f"j{axis}{direction}")
            if result != "":
                ret = -2
            else:
                if block == True:
                    time_left = timeout - (time.time() - t0)
                    ret = self.wait_for_home_or_jog(axis=axis, timeout=time_left)
                    if ret == [0]:
                        ret = 0
                else:
                    ret = 0
        return ret

    # blocks while an axis to finishes homing/jogging
    # axis = -1 blocks while any axis has not finished
    # returns:
    # list of measured lengths of axes, zeros indicate non-homed axes
    # -1 if timeout while waiting for completion
    # -3 if invalid axis
    # -9 if  programming error
    def wait_for_home_or_jog(self, axis=-1, timeout=80):
        ret = -9
        t0 = time.time()
        dims = []
        if axis == -1:
            to_wait_for = self.axes
        else:
            if axis in self.axes:
                to_wait_for = [axis]
            else:
                ret = -3
                to_wait_for = []
        # do waits
        for ax in to_wait_for:
            time_left = timeout - (time.time() - t0)
            ret = -1
            while time_left > 0:
                axl = self.pcb.get(f"l{ax}")
                if (axl is not None) and (axl >= 0):
                    dims += [axl]
                    ret = 0
                    self.current_position[self.axes.index(ax)] = (
                        self.pcb.get(f"r{ax}") / self.steps_per_mm
                    )
                    if axis != -1:
                        self.check_lengths(ax)
                    break
                time_left = timeout - (time.time() - t0)
        if ret == 0:
            ret = dims
        if axis == -1:
            self.check_lengths()
        return ret

    # returns the stage's current position (a list matching the axes input)
    # axis is -1 for all available axes or a list of axes
    # returns None values for axes that could not be read
    def get_position(self, axes=-1):
        ret = []
        if not hasattr(axes, "__len__"):
            if axes == -1:
                axes = self.axes
            else:
                axes = [axes]

        for ax in axes:
            # TODO: probably shouldn't have to do a home check here first
            home_check = self.pcb.get(f"r{ax}")
            if home_check is not None and home_check > 0:
                steps = self.pcb.get(f"r{ax}")
                pos = steps / self.steps_per_mm
            else:
                pos = None
            ret += [pos]
            self.current_position[self.axes.index(ax)] = pos
        return ret

    # makes relative movements
    # mm is a list of offests from the current posision in mm
    # axis is -1 for all available axes or a list of axes you wish to move
    # the mm list and the axis list must match
    # block=True if this is not to return until the movement is complete
    # return codes
    # 0 if there was no error
    # -1 if the timeout expired before the move completed (for block=True mode)
    # -2 if a command was rejected by the firmware (maybe the axes is unhomed or currently homing?)
    # -3 invalid axis
    # -6 attempt to move out of bounds
    # -7 location and axes list length mismatch
    # -8 movement concluded, but we did not reach the goal (stall?)
    # -9 for programming error
    def move(self, mm, axes=-1, block=True, timeout=80):
        """
    moves mm mm, blocks until movement complete, mm can be positive or negative to indicate movement direction
    rejects movements outside limits
    returns 0 upon sucessful move
    """
        ret = -9
        t0 = time.time()

        if not hasattr(mm, "__len__"):
            mm = [mm]

        if not hasattr(axes, "__len__"):
            if axes == -1:
                axes = self.axes
            else:
                axes = [axes]

        if len(mm) != len(axes):
            # raise ValueError("Move error")  #TODO: log movement error
            ret = -7
        else:
            where = [0] * len(mm)  # final locations in mm
            for i, ax in enumerate(axes):
                here = self.pcb.get(f"r{ax}")
                if (here is not None) and (here > 0):
                    where[i] = here / self.steps_per_mm + mm[i]
                else:
                    ret = -2
                    break
            if ret != -2:
                time_left = timeout - (time.time() - t0)
                ret = self.goto(where, axes=axes, block=block, timeout=time_left)
        return ret

    def estop(self, axes=-1):
        """
    Emergency stop of the driver. Unpowers the motor(s)
    """
        ret = -9
        if not hasattr(axes, "__len__"):
            if axes == -1:
                axes = self.axes
            else:
                axes = [axes]
        if len(axes) == self.n_axes:
            result = self.pcb.get("b")
            if result == "":
                ret = 0
            else:
                ret = -2
        else:
            for ax in axes:
                result = self.pcb.get(f"b{ax}")
                if result == "":
                    if ret != -2:  # this error needs to stick
                        ret = 0
                else:
                    ret = -2
        return ret

    # sends the stage somewhere
    # axis is -1 for all available axes or a list of axes you wish to move
    # new_pos is a list of new positions for the axes in mm.
    # this list length must match the axes selected
    # block=True if this is not to return until the movement is complete
    # return codes
    # 0 if there was no error
    # -1 if the timeout expired before the move completed (for block=True mode)
    # -2 if a command was rejected by the firmware (maybe the axes is unhomed or currently homing?)
    # -3 invalid axis
    # -6 attempt to move out of bounds
    # -7 location and axes list length mismatch
    # -8 movement concluded, but we did not reach the goal (stall?)
    # -9 for programming error
    def goto(self, new_pos, axes=-1, block=True, timeout=80):
        """
    goes to an absolute mm position, blocking, returns 0 on success
    """
        ret = -9
        t0 = time.time()
        stop_check_time_res = 0.25  # [s] of often to check if we've stopped

        if not hasattr(new_pos, "__len__"):
            new_pos = [new_pos]

        if not hasattr(axes, "__len__"):
            if axes == -1:
                axes = self.axes
            else:
                axes = [axes]

        if len(new_pos) != len(axes):
            # raise ValueError("Move error")  #TODO: log movement error
            ret = -7
        else:
            # check the new position
            ebs = self.end_buffers * self.steps_per_mm
            for i, ax in enumerate(axes):
                new_pos[i] = round(new_pos[i] * self.steps_per_mm)  # convert to steps
                axl = self.pcb.get(f"l{ax}")
                if (axl is not None) and (axl > 0):
                    axmin = ebs
                    axmax = axl - ebs
                    koz = self.keepout_zones[self.axes.index(ax)]
                    if len(koz) == 0:
                        koz += [-10]  # something that's never enforced for no keepout
                        koz += [-10]
                    koz_min = min(koz) * self.steps_per_mm
                    koz_max = max(koz) * self.steps_per_mm
                    print(
                        f"i={i}, np={new_pos[i]}, axmin={axmin}, axmax={axmax}, kozmin={koz_min}, kozmin={koz_max}"
                    )
                    if (new_pos[i] >= axmin and new_pos[i] <= axmax) and not (
                        new_pos[i] >= koz_min and new_pos[i] <= koz_max
                    ):
                        ret = 0
                    else:
                        ret = -6
                        break
                else:
                    self.homed = False
                    ret = -2
                    break

            if ret == 0:
                # initiate the moves
                for i, ax in enumerate(axes):
                    resp = self.pcb.get(f"g{ax}{new_pos[i]}")
                    if resp == "":
                        ret = 0
                    else:
                        ret = -2
                        break

                if block == True:
                    # now let's wait for all the motion to be done
                    for i, ax in enumerate(axes):
                        q = deque([-1000, -2000], 2)
                        time_left = timeout - (time.time() - t0)
                        while (
                            time_left > 0
                        ):  # while the last two readings are not equal
                            q.append(self.pcb.get(f"r{ax}"))
                            if q[0] == q[1]:
                                if (
                                    q[0] != new_pos[i]
                                ):  # check if we ended where we want to be
                                    print(
                                        f"Goto Wanted:{new_pos[i]/self.steps_per_mm}, Got: {q[0]/self.steps_per_mm}"
                                    )
                                    ret = -8
                                else:
                                    if ret != -8:  # this error case needs to stick
                                        ret = 0
                                break
                            time.sleep(stop_check_time_res)
                            time_left = timeout - (time.time() - t0)
                        if time_left <= 0:
                            ret = -1
                        else:
                            try:
                                self.current_position[i] = (
                                    q[0] / self.steps_per_mm
                                )  # this will fail on an errored position read
                            except:
                                self.current_position[i] = q[0]
        if (ret == 0) and (len(axes) == self.n_axes):
            self.homed = True  # could not have gotten this far without being homed
        return ret

    def close(self):
        pass


if __name__ == "__main__":
    # motion test
    import pcb

    pcb_address = "10.46.0.239"
    with pcb.pcb(pcb_address, ignore_adapter_resistors=True) as p:
        me = us(
            p,
            expected_lengths=[250 - 125],
            keepout_zones=[[20, 30]],
            steps_per_mm=6400,
            extra="",
        )

        print("Connecting")
        result = me.connect()
        if result == 0:
            print("Connected!")
        else:
            raise (ValueError(f"Connection failed with {result}"))
        time.sleep(1)

        if me.homed != True:
            print("Homing required!")
            print("Homing")
            result = me.home()
            if isinstance(result, list):
                print(f"Stage dims = {result}")
            else:
                raise (ValueError(f"Home failed with {result}"))
            time.sleep(1)
        else:
            print("Homing not required!")

        print("GOingTO the middle of the stage")
        mid_mm = [x / 2 / me.steps_per_mm for x in me.measured_lengths]
        result = me.goto(mid_mm)
        if result == 0:
            print("Movement done.")
        else:
            raise (ValueError(f"GOTO failed with {result}"))
        time.sleep(1)

        print("GOingTO keepout zone")
        keepo = [25]
        result = me.goto(keepo)
        if result == 0:
            raise (ValueError("Movement done. (bad)"))
        else:
            print(f"GOTO failed with {result} (yay!)")
        time.sleep(1)

        print("Moving all axes 2cm forward via move")
        move_mm = [20] * me.n_axes
        result = me.move(move_mm)
        if result == 0:
            print("Movement done.")
        else:
            raise (ValueError(f"Move failed with {result}"))
        time.sleep(1)

        print("Moving all axes 2cm backwards via move")
        move_mm = [-20] * me.n_axes
        result = me.move(move_mm)
        if result == 0:
            print("Movement done.")
        else:
            raise (ValueError(f"Move failed with {result}"))
        time.sleep(1)

        print("Jogging")
        result = me.jog(me.axes[0], direction="a")
        if result == 0:
            print("Jogging done.")
        else:
            raise (ValueError(f"Jog failed with {result}"))
        time.sleep(1)

        print("Jogging")
        result = me.jog(me.axes[0], direction="b")
        if result == 0:
            print("Jogging done.")
        else:
            raise (ValueError(f"Jog failed with {result}"))
        time.sleep(1)

        print("Emergency Stopping")
        result = me.estop()
        if result == 0:
            print("Emergency stopped.")
        else:
            raise (ValueError(f"Failed to emergency stop with {result}"))
        time.sleep(10)

        print("Homing")
        result = me.home()
        if isinstance(result, list):
            print(f"Homed. The stage dimensions are {result}")
        else:
            raise (ValueError(f"Failed to home with {result}"))

        me.close()
        print("Test complete.")
