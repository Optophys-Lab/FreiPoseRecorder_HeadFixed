import logging, random
# import cv2
import time
import datetime
from pathlib import Path

from threading import Event, Thread
from queue import Queue, Full

from pypylon import genicam
from pypylon import pylon

from FreiPose_Recorder.utils.VideoWriterFast_gear import VideoWriterFast
from FreiPose_Recorder.utils.VideoWriterFast_gear import QueueOverflow

from FreiPose_Recorder.configs.camera_enums import CameraRegistry

from FreiPose_Recorder.configs.params import TIME_STAMP_STRING, TRIGGER_LINE_IN, MAX_FPS, CONVERT2


import os

NUM_CAMERAS = 2  # simulated cameras
# setup demo environment with emulated cameras
# os.environ["PYLON_CAMEMU"] = f"{NUM_CAMERAS}"
# remove when not needed anymore

CONVERSION_TARGET= {"RGB8":pylon.PixelType_RGB8packed,"Mono8":pylon.PixelType_Mono8}[CONVERT2]

def rel_close(v, max_v, thresh=5.0):
    v_scaled = v / max_v * 100.0
    if 100.0 - v_scaled < thresh:
        return True
    return False


class Recorder(object):
    """Class to handle recording of multiple cameras"""

    def __init__(self, verbosity=0, write_timestamps=False):
        self.write_timestamps = write_timestamps
        self.codec = 'divx'
        self.crf = 0
        self.per_cam_crf = {}
        self.video_writer_list = []  # list of video writers
        self.is_recording = False
        self.is_viewing = False
        self.cams_context = None
        self.multi_record_thread = None
        self.multi_view_queue = None
        self.stop_event = None
        self.error_event = Event()  # event we set if an error occurs to signal the main thread
        self.current_cam = None
        self.current_cam_name = ''
        self.single_view_thread = None
        self.single_view_queue = None
        self.multi_view_thread = None
        self.cams_connected = False
        self.cam_array = []
        self._verbosity = verbosity
        self.save_path = ""
        # self._camera_info = get_camera_infos()
        self._camera_list = list()
        self._camera_names_list = list()

        # self._take_name = 'take'
        self._rid = 0
        self.fps = 10
        self._trigger = None
        self.grab_timeout = 2000 #timeout from start end end of recording to wait for trigger; reduced from 10000 ms
        self.internal_queue_size = 100  # Size of the QUEUE for transfering images between threads

        self.log = logging.getLogger('BaslerRecorder')
        self.log.setLevel(logging.DEBUG)
        self.cameraregistry = CameraRegistry()

    @property
    def fps(self):
        return self._fps

    @fps.setter
    def fps(self, fps_new):
        if fps_new < 1:
            self._fps = 1
        elif fps_new > MAX_FPS:
            self._fps = MAX_FPS
        else:
            self._fps = fps_new

    def get_cam_info(self) -> list:
        self.scan_cams()
        sns = list()
        for idx, cam in enumerate(self.cam_array):
            sns.append(cam.DeviceInfo.GetSerialNumber())
        return sns

    def scan_cams(self):
        """ Searches for attached Basler cams and puts them into our list of cameras. """
        # reset cams
        self._camera_list, self._camera_names_list = list(), list()

        # Get the transport layer factory.
        tlFactory = pylon.TlFactory.GetInstance()

        devices = tlFactory.EnumerateDevices()
        if len(devices) == 0:
            self.log.info('No cameras present')
            self.cam_array = []
            return

        self.log.debug(f'Found {len(devices)} cameras')

        self.cam_array = pylon.InstantCameraArray(len(devices))

        for idx, cam in enumerate(self.cam_array):
            cam.Attach(tlFactory.CreateDevice(devices[idx]))
            sn = cam.DeviceInfo.GetSerialNumber()
            try:
                self.cameraregistry.get_camera(sn)
            except ValueError:
                self.log.warning(f'Connected camera {sn} not found in Enum')

    def connect_cams(self):
        self.cam_array.Open()
        for idx, cam in enumerate(self.cam_array):
            camera_serial = cam.DeviceInfo.GetSerialNumber()
            try:
                c = self.cameraregistry.get_camera(camera_serial)
                self.log.debug(
                    f"set context {c.get('context')} for camera {camera_serial}")
                cam.SetCameraContext(c.get('context'))
                cam.DeviceInfo.SetUserDefinedName(c.get('name'))
            except ValueError:
                r_int = random.randint(10, 256)
                cam.SetCameraContext(r_int)  # for unknown cameras set random context
                cam.DeviceInfo.SetUserDefinedName(f"cam{r_int}")
        self.cams_connected = True
        self.log.debug(f'Connected to {self.cam_array.GetSize()} cameras')

    def disconnect_cams(self):
        """ Disconnects all cameras"""
        if self.cam_array:
            self.cam_array.Close()
        self.cams_connected = False

    def _config_cams_continuous(self, cam):
        if not cam.IsOpen():
            cam.Open()

        # some things are to be set as attributes ...
        try:
            cam.AcquisitionFrameRate.Value = self.fps  # set fps to desired value
        except genicam.LogicalErrorException:
            cam.AcquisitionFrameRateAbs.Value = self.fps

        cam.AcquisitionFrameRateEnable.Value = True

        cam.MaxNumBuffer.SetValue(1024)  # how many buffers there are in total (empty and full)
        cam.OutputQueueSize.SetValue(
            512)  # maximal number of filled buffers (if another image is retrieved it replaces an old one and is called skipped)

        cam.AcquisitionMode.Value = 'Continuous'
        cam.TriggerMode.Value = 'Off'

    def _config_cams_hw_trigger(self, cam):
        if not cam.IsOpen():
            cam.Open()
        # Set rate limiter just above the actual trigger rate to debounce TTL ringing.
        # Ringing produces a spurious second edge ~16-18ms after each real trigger.
        # A cap at fps*1.5 Hz (minimum interval ~22ms) blocks the spurious edge while
        # accepting real triggers at 1/fps (~33ms for 30Hz). Do NOT set to MAX_FPS or
        # disable (False) — either removes the debounce and extra frames appear.
        debounce_fps = self.fps * 1.5
        try:
            cam.AcquisitionFrameRate.Value = debounce_fps
        except genicam.LogicalErrorException:
            cam.AcquisitionFrameRateAbs.Value = debounce_fps
        cam.AcquisitionFrameRateEnable.Value = True
        # behavior wrt to these values is a bit strange to me. Important seems to be to use LastImages Strategy and make MaxNumBuffers larger than OutputQueueSize. Otherwise its not guaranteed to work
        cam.MaxNumBuffer.SetValue(1024)  # how many buffers there are in total (empty and full)
        cam.OutputQueueSize.SetValue(
            512)  # maximal number of filled buffers (if another image is retrieved it replaces an old one and is called skipped)

        cam.AcquisitionMode.Value = 'Continuous'

        #todo move this to camera config? only leave triggermode on ?
        cam.LineSelector.Value = TRIGGER_LINE_IN
        cam.LineMode.Value = "Input"
        #cam.LineSelector = TRIGGER_LINE_OUT
        #cam.LineMode = "Output"
        # cam.LineSource = pylon. # set to exposureactive
        cam.TriggerSelector.Value = "FrameStart"
        cam.TriggerSource.Value = TRIGGER_LINE_IN

        cam.TriggerMode.Value = "On"
        cam.TriggerActivation.Value = 'RisingEdge'

    @staticmethod
    def is_color_cam(cam):
        # get available formats
        available_formats = cam.PixelFormat.Symbolics

        # figure out if a color format is available
        if 'BGR8Packed' in available_formats or 'BGR8' in available_formats:
            return True
        return False

    def set_color_mode(self, cam_id: int, color_mode: str):
        """ Set values for the color mode of the camera from GUI"""
        was_closed = False
        cam = self.cam_array[cam_id]
        if not cam.IsOpen():
            was_closed = True
            cam.Open()
        try:
            cam.PixelFormat.SetValue(color_mode)
        except genicam.LogicalErrorException:
            self.log.info('color mode setting is not available for this camera')
        except genicam.AccessException:
            self.log.info('Cant set color mode while running ! ')
        if was_closed:
            cam.Close()

    def set_gain_exposure(self, cam_id: int, gain: float, exposure: float):
        """ Set values from GUI"""
        was_closed = False
        cam = self.cam_array[cam_id]
        if not cam.IsOpen():
            was_closed = True
            cam.Open()
        try:
            cam.Gain.SetValue(gain)
        except genicam.OutOfRangeException:
            self.log.warning(f'Value{gain:0.0f} is out of range of gain for this camera')
        except genicam.LogicalErrorException:
            self.log.info('gain setting is not available for this camera')

        try:
            cam.ExposureTime.SetValue(exposure)
        except genicam.OutOfRangeException:
            self.log.warning(f'Value{exposure:0.0f} is out of range of the exposure time')
        except genicam.LogicalErrorException:
            try:
                cam.ExposureTimeAbs.SetValue(exposure)
            except genicam.LogicalErrorException:
                self.log.info('Exposure time  setting is not available for this camera')
        if was_closed:
            cam.Close()

    def run_white_balance(self, cam_id: int):
        """Set auto white balance for color cameras"""
        was_closed = False
        cam = self.cam_array[cam_id]
        if not cam.IsOpen():
            was_closed = True
            cam.Open()
        c = self.cameraregistry.get_camera(cam.DeviceInfo.GetSerialNumber())
        self.log.info(f"Setting white balance for {c.get('name')} "
                      f"{cam.GetDeviceInfo().GetSerialNumber()}")

        if not self.is_color_cam(cam):
            self.log.info(f"{c.get('name')} is not a color cam\n"
                          f"Skipping white balancing")
            return
        try:
            cam.AutoFunctionROISelector.SetValue('ROI1')
            cam.AutoFunctionROIUseWhiteBalance.SetValue(False)
            cam.AutoFunctionROISelector.SetValue('ROI2')
            cam.AutoFunctionROIUseWhiteBalance.SetValue(True)

            # define ROI to use
            cam.AutoFunctionROISelector.SetValue('ROI2')
            cam.AutoFunctionROIWidth.SetValue(cam.Width.GetValue())
            cam.AutoFunctionROIHeight.SetValue(cam.Height.GetValue())
            cam.AutoFunctionROIOffsetX.SetValue(0)
            cam.AutoFunctionROIOffsetY.SetValue(0)
        except genicam.LogicalErrorException:
            self.log.info('White balance setting is not available for this camera')
            return

        # get initial values
        if self._verbosity > 1:
            print('Initial balance ratio:', end='\t')
            cam.BalanceRatioSelector.SetValue('Red')
            print('Red= ', cam.BalanceRatio.GetValue(), end='\t')
            cam.BalanceRatioSelector.SetValue('Green')
            print('Green= ', cam.BalanceRatio.GetValue(), end='\t')
            cam.BalanceRatioSelector.SetValue('Blue')
            print('Blue= ', cam.BalanceRatio.GetValue())

        cam.BalanceWhiteAuto.SetValue('Once')

        i = 0
        while not cam.BalanceWhiteAuto.GetValue() == 'Off':
            if not cam.IsGrabbing():
                cam.GrabOne(5000)
                i += 1

                if i > 100:
                    self.log.error('Auto White balance was not successful')
                    break
            else:
                time.sleep(0.01)
                i = 0

        # get final values
        if self._verbosity > 1:
            print('Final balance ratio:', end='\t')
            cam.BalanceRatioSelector.SetValue('Red')
            print('Red= ', cam.BalanceRatio.GetValue(), end='\t')
            cam.BalanceRatioSelector.SetValue('Green')
            print('Green= ', cam.BalanceRatio.GetValue(), end='\t')
            cam.BalanceRatioSelector.SetValue('Blue')
            print('Blue= ', cam.BalanceRatio.GetValue())
        self.log.debug('Finished White balancing')
        if was_closed:
            cam.Close()

    def run_auto_exposure(self, cam_id: int) -> float:
        """ Adjust exposure time while keeping gain fixed. """
        was_closed = False
        cam = self.cam_array[cam_id]
        if not cam.IsOpen():
            was_closed = True
            cam.Open()

        self.log.info(f"Setting auto exposure for {self.cameraregistry.get_camera(cam.GetDeviceInfo().GetSerialNumber()).get('name')}"
                      f"{cam.GetDeviceInfo().GetSerialNumber()}")
        try:
            cam.AutoFunctionROISelector.SetValue('ROI1')
            cam.AutoFunctionROIUseBrightness.SetValue(True)
            cam.AutoFunctionROISelector.SetValue('ROI2')
            cam.AutoFunctionROIUseBrightness.SetValue(False)
        except genicam.LogicalErrorException:
            self.log.info('Auto exposure setting is not available for this camera')
            return 0

        # define ROI to use
        cam.AutoFunctionROISelector.SetValue('ROI1')
        wOff = int(cam.Width.GetValue() / 4)
        hOff = int(cam.Height.GetValue() / 4)
        wSize = int(cam.Width.GetValue() / 2)
        hSize = int(cam.Height.GetValue() / 2)

        # Enforce size is a multiple of two (important because of bayer pattern)
        wSize = int(wSize / 2) * 2
        hSize = int(hSize / 2) * 2

        # set ROI
        cam.AutoFunctionROIWidth.SetValue(wSize)
        cam.AutoFunctionROIHeight.SetValue(hSize)
        cam.AutoFunctionROIOffsetX.SetValue(wOff)
        cam.AutoFunctionROIOffsetY.SetValue(hOff)

        # 0.3 means that the target brightness is 30 % of the maximum brightness
        # of the raw pixel value read out from the sensor.
        cam.AutoTargetBrightness.SetValue(0.2)

        # give auto some bounds
        cam.AutoExposureTimeLowerLimit.SetValue(cam.AutoExposureTimeLowerLimit.GetMin())
        cam.AutoExposureTimeUpperLimit.SetValue(cam.AutoExposureTimeUpperLimit.GetMax())

        # set gain to its reference value
        cam.ExposureAuto.SetValue('Once')

        i = 0
        while not cam.ExposureAuto.GetValue() == 'Off':
            if not cam.IsGrabbing():
                cam.GrabOne(5000)
                # instead of grabing just waiting if cama is already grabbing ?
                i += 1

                if i > 100:
                    self.log.error('Auto Exposure was not successful')
                    break
            else:
                time.sleep(0.01)
                i = 0
        exposure_time = self.get_cam_exposureTime(cam)
        self.log.debug(f'Final exposure after {i} images {exposure_time:0.1f} us in range '
                       f'{cam.AutoExposureTimeLowerLimit.GetMin()}-{cam.AutoExposureTimeUpperLimit.GetMax()}')

        # check if we should give warnings
        if rel_close(exposure_time, cam.AutoExposureTimeUpperLimit.GetMax()):
            self.log.warning('Final exposure value is very close to its maximum value:'
                             'Consider increasing gain, opening camera shutter wider or put more light.')

        if was_closed:
            cam.Close()
        return exposure_time

    def run_auto_gain(self, cam_id) -> float:
        # check if this can be run while visualization is running ?
        # do a check if grabbing is already grabbing ?
        # if so just wait until th flag gets reset ?
        """ Adjust gain while keeping exposure time fixed.
        returns resulting gain
        """
        was_closed = False
        cam = self.cam_array[cam_id]
        if not cam.IsOpen():
            was_closed = True
            cam.Open()
        self.log.info(f"Setting auto gain for {self.cameraregistry.get_camera(cam.GetDeviceInfo().GetSerialNumber()).get('name','Unknown')} "
                      f"{cam.GetDeviceInfo().GetSerialNumber()}")

        # no clue whether those are needed / or work
        try:
            cam.AutoFunctionROISelector.SetValue('ROI1')
            cam.AutoFunctionROIUseBrightness.SetValue(True)
            cam.AutoFunctionROISelector.SetValue('ROI2')
            cam.AutoFunctionROIUseBrightness.SetValue(False)
        except genicam.LogicalErrorException:
            self.log.info('Auto gain setting is not available for this camera')
            return 0

        # define ROI to use
        cam.AutoFunctionROISelector.SetValue('ROI1')
        wOff = int(cam.Width.GetValue() / 4)
        hOff = int(cam.Height.GetValue() / 4)
        wSize = int(cam.Width.GetValue() / 2)
        hSize = int(cam.Height.GetValue() / 2)

        # Enforce size is a multiple of two (important because of bayer pattern)
        wSize = int(wSize / 2) * 2
        hSize = int(hSize / 2) * 2

        # set ROI
        cam.AutoFunctionROIWidth.SetValue(wSize)
        cam.AutoFunctionROIHeight.SetValue(hSize)
        cam.AutoFunctionROIOffsetX.SetValue(wOff)
        cam.AutoFunctionROIOffsetY.SetValue(hOff)

        # 0.3 means that the target brightness is 30 % of the maximum brightness
        # of the raw pixel value read out from the sensor.
        cam.AutoTargetBrightness.SetValue(0.2)

        # give auto some bounds
        cam.AutoGainLowerLimit.SetValue(cam.Gain.GetMin())
        cam.AutoGainUpperLimit.SetValue(cam.Gain.GetMax())

        # set exposure to its reference value
        # cam.ExposureTime.SetValue(0)
        # print('Initial gain', cam.Gain.GetValue())
        cam.GainAuto.SetValue('Once')
        i = 0
        while not cam.GainAuto.GetValue() == 'Off':
            if not cam.IsGrabbing():
                cam.GrabOne(5000)
                # instead of grabing just waiting if cama is already grabbing ?
                i += 1

                if i > 100:
                    self.log.error('Auto Gain was not successful')
                    break
            else:
                time.sleep(0.01)
                i = 0
        gain = self.get_cam_gain(cam)
        self.log.debug(f'Final gain after {i} images {cam.Gain.GetValue():0.1f} in range {cam.Gain.GetMin()}'
                       f'-{cam.Gain.GetMax()}')

        # check if we should give warnings
        if rel_close(gain, cam.Gain.GetMax()):
            self.log.warning('Final gain value is very close to its maximum value:'
                             ' Consider increasing exposure time, opening camera shutter wider or put more light.')

        if was_closed:
            cam.Close()
        return gain

    @staticmethod
    def get_cam_exposureTime(cam: pylon.InstantCamera) -> float:
        """Wrapper to ge exposure time.. in some cameras has different node"""
        try:
            return cam.ExposureTime.GetValue()
        except genicam.LogicalErrorException:
            return cam.ExposureTimeAbs.GetValue()

    @staticmethod
    def set_cam_exposureTime(cam: pylon.InstantCamera, exp_time: [float, None]):
        """Wrapper to set exposure time, in some cameras has different node"""
        if exp_time is None:
            return
        try:
            cam.ExposureTime.SetValue(exp_time)
        except genicam.OutOfRangeException:
            print(f'Value{exp_time:0.0f} is out of range of the exposure time')
            try:
                cam.ExposureTimeAbs.SetValue(exp_time)
            except genicam.LogicalErrorException:
                pass  # Not implemented for this camera

    @staticmethod
    def get_cam_gain(cam: pylon.InstantCamera) -> float:
        """Wrapper to get gain.. some cameras might not have the option"""
        try:
            return cam.Gain.GetValue()
        except genicam.LogicalErrorException:
            return 0

    @staticmethod
    def get_cam_limits(cam: pylon.InstantCamera) -> [tuple, tuple, list]:
        try:
            gain_limits = (cam.Gain.GetMin(), cam.Gain.GetMax())
            exp_limits = (cam.ExposureTime.GetMin(), cam.ExposureTime.GetMax())
            color_modes = cam.PixelFormat.Symbolics
            return gain_limits, exp_limits, color_modes
        except genicam.LogicalErrorException:
            return [], [], []

    @classmethod
    def set_cam_settings(cls, cam: pylon.InstantCamera, settings: dict):
        was_closed = False
        if not cam.IsOpen():
            was_closed = True
            cam.Open()

        try:
            gain = settings['gain']
            cam.Gain.SetValue(gain)
        except genicam.OutOfRangeException:
            print(f'Value{gain:0.0f} is out of range of gain for this camera')
        except genicam.LogicalErrorException:
            pass  # Not implemented for this camera
        except KeyError:
            pass

        cls.set_cam_exposureTime(cam, settings.get('exp_time', None))

        try:
            flipX = settings['flipX']
            cam.ReverseX.SetValue(flipX)
        except genicam.LogicalErrorException:
            pass  # Not implemented for this camera
        except KeyError:
            pass  # not in settings

        try:
            flipY = settings['flipY']
            cam.ReverseY.SetValue(flipY)
        except genicam.LogicalErrorException:
            pass  # Not implemented for this camera
        except KeyError:
            pass  # not in settings

        try:
            red_balance, green_balance, blue_balance = settings['color_balance']
            cam.BalanceRatioSelector.SetValue('Red')
            cam.BalanceRatio.SetValue(red_balance)
            cam.BalanceRatioSelector.SetValue('Green')
            cam.BalanceRatio.SetValue(green_balance)
            cam.BalanceRatioSelector.SetValue('Blue')
            cam.BalanceRatio.SetValue(blue_balance)
        except genicam.LogicalErrorException:
            pass  # Not implemented for this camera
        except KeyError:
            pass  # not in settings

        try:
            cam.PixelFormat.SetValue(settings['color_mode'])
        except genicam.LogicalErrorException: # Not implemented for this camera
            print('Could not set color mode')
        except genicam.InvalidArgumentException:
            print(f"Color mode {settings['color_mode']} not available for {cam.DeviceInfo.GetUserDefinedName()} camera")

        line_in = settings.get('lineIN', None)
        if line_in is None:
            line_in = TRIGGER_LINE_IN
        try:
            cam.LineSelector.Value = line_in
            cam.LineMode.Value = "Input"
            cam.TriggerSelector.Value = "FrameStart"
            cam.TriggerSource.Value = line_in
        except genicam.LogicalErrorException:
            print('Could not set trigger line')

        line_out = settings.get('lineOUT', None)
        if line_out:
            cam.LineSelector.Value = line_out
            cam.LineMode.Value = "Output"
            # set to exposureactive
            cam.LineSource.Value = "ExposureActive"

        if was_closed:
            cam.Close()

    @classmethod
    def get_cam_settings(cls, cam: pylon.InstantCamera) -> dict:
        """
        Get settings from a camera
        :param cam: camera object
        :return: dict with settings
        """
        was_closed = False
        if not cam.IsOpen():
            was_closed = True
            cam.Open()

        cam_settings = {}
        cam_name = cam.DeviceInfo.GetUserDefinedName()
        cam_settings[cam_name] = {}

        cam_settings[cam_name]['gain'] = cls.get_cam_gain(cam)
        cam_settings[cam_name]['exp_time'] = cls.get_cam_exposureTime(cam)

        try:
            cam_settings[cam_name]['flipX'] = cam.ReverseX.GetValue()
            cam_settings[cam_name]['flipY'] = cam.ReverseY.GetValue()
        except genicam.LogicalErrorException:
            pass  # Not implemented for this camera

        try:
            if cls.is_color_cam(cam):
                cam.BalanceRatioSelector.SetValue('Red')
                red_balance = cam.BalanceRatio.GetValue()
                cam.BalanceRatioSelector.SetValue('Green')
                green_balance = cam.BalanceRatio.GetValue()
                cam.BalanceRatioSelector.SetValue('Blue')
                blue_balance = cam.BalanceRatio.GetValue()
                cam_settings[cam_name]['color_balance'] = (red_balance, green_balance, blue_balance)
        except genicam.LogicalErrorException:
            pass  # Not implemented for this camera

        try:
            color_mode = cam.PixelFormat.GetValue()
            cam_settings[cam_name]['color_mode'] = color_mode
        except genicam.LogicalErrorException:
            pass  # Not implemented for this camera

        # if implemet savign and setting lines need to cycle trough all of them
        #problem need to run trough all lines to get all settings

        if was_closed:
            cam.Close()
        return cam_settings

    def flip_image_x(self, cam_id: int):
        """ Flips the image  of a single camera in the X plane """
        was_closed = False
        cam = self.cam_array[cam_id]
        if not cam.IsOpen():
            was_closed = True
            cam.Open()
        cam.ReverseX.Value = cam.ReverseX.GetValue()  # switch value
        if was_closed:
            cam.Close()

    def flip_image_y(self, cam_id: int):
        """ Flips the image of a single camera in the Y plane"""
        was_closed = False
        cam = self.cam_array[cam_id]
        if not cam.IsOpen():
            was_closed = True
            cam.Open()
        cam.ReverseY.Value = not cam.ReverseY.GetValue()  # switch value
        if was_closed:
            cam.Close()

    def run_single_cam_show(self, cam_id: int, stop_event: Event):
        """
        Show a single camera in a separate thread
        :param cam_id: camera id
        :param stop_event: Event to stop the thread
        """
        cam = self.cam_array[cam_id]
        self.single_view_queue = Queue(self.internal_queue_size) # QUEUE for transferring images between threads
        if not cam.IsOpen():
            cam.Open()
        try:
            cam_name = self.cameraregistry.get_camera(cam.GetDeviceInfo().GetSerialNumber()).get('name', 'unknown')
            self.log.info(f'Showing device {cam_name} '
                          f'with {self.fps} FPS')
            self.current_cam_name = cam_name
        except ValueError:
            self.log.info(f'Showing device {cam.GetDeviceInfo().GetSerialNumber()} with {self.fps} FPS')
            self.current_cam_name = cam.GetDeviceInfo().GetSerialNumber()

        self._config_cams_continuous(cam)
        self.current_cam = cam

        self.stop_event = stop_event
        self.error_event.clear()
        self.single_view_thread = Thread(target=self.single_cam_show)
        self.single_view_thread.start()

    def stop_single_cam_show(self):
        self.log.debug('Stopping single view, waiting for join')
        self.single_view_thread.join()  # wait for thread to finish
        self.log.debug('single view thread has joined')
        self.current_cam = None
        self.error_event.clear()
        self.stop_event = None
        self.single_view_thread = None

    def single_cam_show(self):
        """
        Get images from a single camera in a separate thread. Puts images to Q for visualization in GUI
        """
        cam = self.current_cam
        converter = pylon.ImageFormatConverter()
        converter.OutputPixelFormat = pylon.PixelType_RGB8packed
        converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned  # most significant bit first #
        # LsbAligned other option

        cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

        while not self.stop_event.is_set():
            try:
                grabResult = cam.RetrieveResult(self.grab_timeout, pylon.TimeoutHandling_ThrowException)
                if grabResult.GrabSucceeded():
                    #is this line needed ?
                    targetImage = pylon.PylonImage.Create(pylon.PixelType_Mono8, grabResult.GetWidth(),
                                                          grabResult.GetHeight())

                    if converter.ImageHasDestinationFormat(grabResult):
                        # no conversion required
                        img = grabResult.GetArray()
                    else:
                        # convert to RGB
                        targetImage = converter.Convert(grabResult)
                        img = targetImage.GetArray()

                    self.single_view_queue.put_nowait(img)
                    grabResult.Release()
                else:
                    self.log.error(f"Occured: {grabResult.ErrorCode} {grabResult.ErrorDescription}")
            except genicam.TimeoutException as e:
                self.log.error(e)
                self.error_event.set()
                break
            except Full:
                self.log.error("Queue buffer overrun !")
                self.error_event.set()
                break
            except Exception as e:  # some other error occured
                self.log.error(e)
                self.error_event.set()
                break
        cam.StopGrabbing()

    def run_multi_cam_show(self, stop_event: Event, use_hw_trigger: bool = False):
        self.multi_view_queue = [Queue(self.internal_queue_size) for _ in range(self.cam_array.GetSize())]
        #self.multi_view_queue = [Queue(self.internal_queue_size)] * self.cam_array.GetSize()
        #second option does copies of ques and not references!

        if not self.cam_array.IsOpen():
            self.cam_array.Open()

        self.log.info(f'Showing {self.cam_array.GetSize()} cameras '
                      f'with {self.fps} FPS')

        self.cams_context = {} # to identify from which camera the images arrive

        for c_id, cam in enumerate(self.cam_array):
            if use_hw_trigger:
                self._config_cams_hw_trigger(cam)
            else:
                self._config_cams_continuous(cam)
            self.cams_context[cam.GetCameraContext()] = c_id
        #self.log.debug(self.cams_context)
        self.stop_event = stop_event
        self.error_event.clear()
        self.multi_view_thread = Thread(target=self.multi_cam_show)
        self.multi_view_thread.start()
        self.is_viewing = True

    def stop_multi_cam_show(self):
        if self.multi_view_thread:
            self.log.debug('Stopping multi-view, waiting for join')
            self.multi_view_thread.join()  # wait for thread to finish
            self.log.debug('multi-view thread joined')
        self.stop_event = None
        self.error_event.clear()
        self.multi_view_thread = None
        self.cams_context = None
        self.is_viewing = False

    def multi_cam_show(self):
        converter = pylon.ImageFormatConverter()
        converter.OutputPixelFormat = pylon.PixelType_RGB8packed
        converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned  # most significant bit first #
        # will i need an array of converters ?

        self.cam_array.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        while not self.stop_event.is_set():
            try:
                grabResult = self.cam_array.RetrieveResult(self.grab_timeout, pylon.TimeoutHandling_ThrowException)
                context_id = self.cams_context[grabResult.GetCameraContext()]
                #self.log.debug(f"Cam {grabResult.GetCameraContext()} grabbed with context {context_id}")

                if grabResult.GrabSucceeded():
                    if converter.ImageHasDestinationFormat(grabResult):
                        # no conversion required
                        img = grabResult.GetArray()
                    else:
                        # convert to RGB
                        targetImage = converter.Convert(grabResult)
                        img = targetImage.GetArray()
                    #img = grabResult.GetArray()
                    # context_id = self.cams_context[grabResult.GetCameraContext()]
                    self.multi_view_queue[context_id].put_nowait(img[::2, ::2])
                    grabResult.Release()
                else:
                    print("Error: ", grabResult.ErrorCode, grabResult.ErrorDescription)
            except genicam.TimeoutException as e:
                self.log.error(e)
                self.error_event.set()
                break
            except Full:
                self.log.error(f"Queue buffer for camera {context_id} overrun !")
                self.error_event.set()
                break
        self.cam_array.StopGrabbing()
        self.is_viewing = False

    def run_multi_cam_record(self, stop_event: Event, filename: str = 'testrec', use_hw_trigger: bool = False):
        was_closed = False
        self.multi_view_queue = [Queue(self.internal_queue_size) for _ in range(self.cam_array.GetSize())]

        # create path if not exists
        (Path(self.save_path)).mkdir(parents=True, exist_ok=True)

        if not self.cam_array.IsOpen():
            was_closed = True
            self.cam_array.Open()

        self.log.info(f'Recording {self.cam_array.GetSize()} cameras '
                      f'with {self.fps} FPS')

        self.cams_context = {}
        self.video_writer_list = list()
        try:
            timestamp = datetime.datetime.now().strftime(TIME_STAMP_STRING)
        except (TypeError, ValueError):
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

        # to make sure all have the same timestamp
        for c_id, cam in enumerate(self.cam_array):
            if use_hw_trigger:
                self._config_cams_hw_trigger(cam)
            else:
                self._config_cams_continuous(cam)

            self.cams_context[cam.GetCameraContext()] = c_id
            cam_name = cam.DeviceInfo.GetUserDefinedName()
            video_name = f"{filename}_{timestamp}_{cam_name}.mp4"
            video_name = (Path(self.save_path) / video_name).as_posix()
            cam_crf = self.per_cam_crf.get(cam_name, self.crf)
            self.video_writer_list.append(VideoWriterFast(video_name,
                                                          fps=self.fps,
                                                          codec=self.codec,
                                                          crf=cam_crf))
        # self.log.debug(print(self.cams_context))
        self.stop_event = stop_event
        self.error_event.clear()
        self.multi_record_thread = Thread(target=self.multi_cam_record)
        self.multi_record_thread.start()
        self.is_recording = True

    def stop_multi_cam_record(self):
        self.log.debug('Stopping recording, waiting for join')
        self.multi_record_thread.join()
        self.log.debug('thread joined,waiting for writers to finish')
        for writer in self.video_writer_list:
            writer.wait_to_finish()
            writer.stop()
        self.log.debug('writers finished')
        self.is_recording = False
        self.error_event.clear()
        self.stop_event = None
        self.multi_record_thread = None
        self.cams_context = None

    def multi_cam_record(self):
        self.cam_array.StartGrabbing(pylon.GrabStrategy_LatestImages)
        # cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)  # here you dont have any buffer
        # cam.StartGrabbing(pylon.GrabStrategy_OneByOne)  # here you dont get warnings if something gets skipped

        while not self.stop_event.is_set():
            try:
                grabResult = self.cam_array.RetrieveResult(self.grab_timeout, pylon.TimeoutHandling_ThrowException)
                context_id = self.cams_context[grabResult.GetCameraContext()]
                #self.log.debug(f"Cam {grabResult.GetCameraContext()} grabbed with context {context_id}")

                if grabResult.GetNumberOfSkippedImages() > 0:
                    self.log.warning(f'Cam{context_id}: Missed {grabResult.GetNumberOfSkippedImages()} frames')
                if grabResult.GrabSucceeded():
                    img = grabResult.GetArray()
                    img_nr_camera = grabResult.ID
                    img_nr = grabResult.ImageNumber
                    img_ts = grabResult.TimeStamp

                    # context_id = self.cams_context[grabResult.GetCameraContext()]
                    if self.write_timestamps:
                        self.video_writer_list[context_id].feed((img, img_nr_camera, img_nr, img_ts))
                    else:
                        self.video_writer_list[context_id].feed(img)
                    try:
                        self.multi_view_queue[context_id].put_nowait(img[::2, ::2])
                    except (Full, QueueOverflow):
                        pass  # GUI preview can't keep up — drop preview frame, recording continues
                    grabResult.Release()
                else:
                    self.log.error(grabResult.ErrorCode, grabResult.ErrorDescription)

            except genicam.TimeoutException as e:
                if self.stop_event.is_set():
                    # Expected: RetrieveResult timed out after intentional stop, no new triggers
                    break
                cam_ctx = grabResult.GetCameraContext() if 'grabResult' in dir() else '?'
                self.log.error(f"Camera timeout on cam {cam_ctx} after {self.grab_timeout}ms: {e}")
                self.error_event.set()
                break
        self.cam_array.StopGrabbing()
        self.is_recording = False


if __name__ == "__main__":
    baslerRec = Recorder()
    print(baslerRec.get_cam_info())
