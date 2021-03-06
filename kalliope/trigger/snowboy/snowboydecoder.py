#!/usr/bin/env python

import collections
from threading import Thread

import pyaudio
from . import snowboydetect
import time
import os
import logging
logging.basicConfig()
logger = logging.getLogger("kalliope")
TOP_DIR = os.path.dirname(os.path.abspath(__file__))

RESOURCE_FILE = os.path.join(TOP_DIR, "resources/common.res")

class SnowboyOpenAudioException(Exception):
    pass


class RingBuffer(object):
    """Ring buffer to hold audio from PortAudio"""
    def __init__(self, size = 4096):
        self._buf = collections.deque(maxlen=size)
        self.paused = False

    def extend(self, data):
        """Adds data to the end of buffer"""
        if not self.paused:
            self._buf.extend(data)

    def get(self):
        """Retrieves data from the beginning of buffer and clears it"""
        tmp = bytes(bytearray(self._buf))
        self._buf.clear()
        return tmp

    def pause(self):
        self.paused = True

    def unpause(self):
        self.paused = False

class HotwordDetector(Thread):
    """
    Snowboy decoder to detect whether a keyword specified by `decoder_model`
    exists in a microphone input stream.

    :param decoder_model: decoder model file path, a string or a list of strings
    :param resource: resource file path.
    :param sensitivity: decoder sensitivity, a float of a list of floats.
                              The bigger the value, the more senstive the
                              decoder. If an empty list is provided, then the
                              default sensitivity in the model will be used.
    :param audio_gain: multiply input volume by this factor.
    :param apply_frontend: applies the frontend processing algorithm if True.
    """
    def __init__(self, 
                 decoder_model, 
                 resource=RESOURCE_FILE, 
                 sensitivity=[], 
                 audio_gain=1,
                 apply_frontend=False,
                 detected_callback=None,
                 interrupt_check=lambda: False):

        super(HotwordDetector, self).__init__()
        self.detected_callback = detected_callback
        self.interrupt_check = interrupt_check
        self.sleep_time = 0.03
        self.kill_received = False
        self.paused = False

        def audio_callback(in_data, frame_count, time_info, status):
            self.ring_buffer.extend(in_data)
            play_data = chr(0) * len(in_data)
            return play_data, pyaudio.paContinue

        tm = type(decoder_model)
        ts = type(sensitivity)
        if tm is not list:
            decoder_model = [decoder_model]
        if ts is not list:
            sensitivity = [sensitivity]
        model_str = ",".join(decoder_model)

        self.detector = snowboydetect.SnowboyDetect(
            resource_filename=resource.encode(), model_str=model_str.encode())
        self.detector.SetAudioGain(audio_gain)
        self.detector.ApplyFrontend(apply_frontend)
        self.num_hotwords = self.detector.NumHotwords()
        
        if len(sensitivity) > self.num_hotwords:            # If more sensitivities available as hotwords, it will raise an AssertionError
            assert self.num_hotwords == len(sensitivity), \
                "number of hotwords in decoder_model (%d) and sensitivity " \
                "(%d) does not match" % (self.num_hotwords, len(sensitivity))

        if len(sensitivity) != self.num_hotwords:           # Some umdl model contains more then one keyword.
            sensitivity_match_hotwords = False             # If the user sets only one sensitivity, we add for the second model a default sensitivity of 0.5
            while not sensitivity_match_hotwords:
                sensitivity.append(0.5)
                if len(sensitivity) == self.num_hotwords:
                    sensitivity_match_hotwords = True
        
        if len(decoder_model) > 1 and len(sensitivity) == 1:
            sensitivity = sensitivity*self.num_hotwords

        sensitivity_str = ",".join([str(t) for t in sensitivity])
        if len(sensitivity) != 0:
            self.detector.SetSensitivity(sensitivity_str.encode())

        self.ring_buffer = RingBuffer(
            self.detector.NumChannels() * self.detector.SampleRate() * 5)
        self.audio = pyaudio.PyAudio()
        self.open_audio(audio_callback)

    def open_audio(self, audio_callback, i=0):
        try:
            self.stream_in = self.audio.open(
                input=True, output=False,
                format=self.audio.get_format_from_width(
                    self.detector.BitsPerSample() / 8),
                channels=self.detector.NumChannels(),
                rate=self.detector.SampleRate(),
                frames_per_buffer=2048,
                stream_callback=audio_callback)
        except IOError as error:
            logger.debug("IOError raised, i = %s (error: %s)"
                         % (i, repr(error)))
            if i == 5:
                # Let's give up...
                raise SnowboyOpenAudioException(
                    'Error while trying to open audio: %',
                    repr(error))

            i = i + 1
            time.sleep(i)
            self.open_audio(audio_callback, i)

    def run(self):
        """
        Start the voice detector. For every `sleep_time` second it checks the
        audio buffer for triggering keywords. If detected, then call
        corresponding function in `detected_callback`, which can be a single
        function (single model) or a list of callback functions (multiple
        models). Every loop it also calls `interrupt_check` -- if it returns
        True, then breaks from the loop and return.

        :param detected_callback: a function or list of functions. The number of
                                  items must match the number of models in
                                  `decoder_model`.
        :param interrupt_check: a function that returns True if the main loop
                                needs to stop.
        :param float sleep_time: how much time in second every loop waits.
        :return: None
        """
        if self.interrupt_check():
            logger.debug("detect voice return")
            return

        tc = type(self.detected_callback)
        if tc is not list:
            self.detected_callback = [self.detected_callback]
        if len(self.detected_callback) == 1 and self.num_hotwords > 1:
            self.detected_callback *= self.num_hotwords

        assert self.num_hotwords == len(self.detected_callback), \
            "Error: hotwords in your models (%d) do not match the number of " \
            "callbacks (%d)" % (self.num_hotwords, len(self.detected_callback))

        logger.debug("detecting...")

        while not self.kill_received:
            if not self.paused:
                if self.interrupt_check():
                    logger.debug("detect voice break")
                    break
                data = self.ring_buffer.get()
                if len(data) == 0:
                    time.sleep(self.sleep_time)
                    continue

                ans = self.detector.RunDetection(data)
                if ans == -1:
                    logger.warning("Error initializing streams or reading audio data")
                elif ans > 0: 
                    message = "Keyword %s detected" % ans
                    logger.debug(message)
                    callback = self.detected_callback[ans-1]
                    if callback is not None:
                        callback()
            else:
                # take a little break
                time.sleep(self.sleep_time)

        logger.debug("[Snowboy] process finished.")

    def terminate(self):
        """
        Terminate audio stream. Users cannot call start() again to detect.
        :return: None
        """
        self.stream_in.stop_stream()
        self.stream_in.close()
        self.audio.terminate()
        logger.debug("[Snowboy] Audio stream cleaned.")

    def pause(self):
        self.paused = True
        self.ring_buffer.pause()

    def unpause(self):
        self.paused = False
        self.ring_buffer.unpause()