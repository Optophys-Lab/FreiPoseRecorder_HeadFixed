# import the necessary packages
import json
import logging
from pathlib import Path
from threading import Thread
import time
from vidgear.gears import WriteGear
from queue import Queue

class QueueOverflow(Exception):
   """Base class for other exceptions"""
   pass


class VideoWriterFast:
    """
    Utility for faster Video writing with VideoGear.
    Basically runs writing of frames in an separate thread.
    """
    def __init__(self, video_path, fps, codec="libx264", crf=0, queue_size=512):
        self.crf = crf
        self.fps = fps
        self.codec = codec
        self.video_path = video_path

        # initialize the file video stream along with the boolean
        # used to indicate if the thread should be stopped or not
        self.stream = None  # this is initialized when we get the first frame
        self.stopped = False

        self.started = False

        self.write_speed = None

        # initialize the queue used to store frames read from
        # the video file
        self.queue_size = queue_size
        self.Q = Queue(maxsize=queue_size)
        self.frame_ts = []
        # intialize thread
        self.thread = Thread(target=self.update, args=())
        self.thread.daemon = True

        # perf monitoring
        self._cam_name = Path(video_path).stem.split('_')[-1]
        self.log = logging.getLogger(f'VideoWriter.{self._cam_name}')
        self._perf_log_path = video_path.replace('.mp4', '.perf.csv')
        self._perf_file = None
        self._frames_written = 0
        self._overflow_count = 0
        self._overflows_since_last_stat = 0
        self._start_time = None
        self._last_stat_time = None
        self._stat_interval = 10.0  # seconds between perf log rows

    def start(self):
        """starts the thread to write frames to the video file"""
        self.started = True
        self._perf_file = open(self._perf_log_path, 'w', buffering=1)
        self._perf_file.write('elapsed_s,frames_written,queue_depth,queue_pct,write_fps,overflows_since_last\n')
        self.thread.start()
        return self

    def update(self):
        """"""
        # keep looping infinitely
        while True:
            # if the thread indicator variable is set, stop the
            # thread
            if self.stopped:
                break

            # otherwise, ensure the there is something in the queue and the stream was initialized
            if self.Q.qsize() > 0 and self.stream is not None:
                # get the next frame from the queue
                frame = self.Q.get()

                start = time.time()
                # write to stream
                try:
                    self.stream.write(frame, rgb_mode=False)
                except ValueError as e:
                    self.stopped = True
                    print("Error writing frame to stream: {}".format(e))
                now = time.time()
                if self.write_speed is None:
                    self.write_speed = now - start
                else:
                    self.write_speed = 0.85*self.write_speed + 0.15*(now - start)

                self._frames_written += 1
                if self._start_time is None:
                    self._start_time = now
                    self._last_stat_time = now
                elif now - self._last_stat_time >= self._stat_interval:
                    self._emit_perf_stat(now)

            else:
                time.sleep(0.001)  # Rest for 1ms, we have an empty queue

        if self._start_time is not None:
            self._emit_perf_stat(time.time())  # final stat row at stop
        if self._perf_file is not None:
            self._perf_file.close()
        self.stream.close()

    def _emit_perf_stat(self, now):
        elapsed = now - self._start_time if self._start_time else 0.0
        q_depth = self.Q.qsize()
        q_pct = 100.0 * q_depth / self.queue_size
        write_fps = (1.0 / self.write_speed) if self.write_speed else 0.0
        row = f"{elapsed:.1f},{self._frames_written},{q_depth},{q_pct:.1f},{write_fps:.2f},{self._overflows_since_last_stat}\n"
        if self._perf_file is not None:
            self._perf_file.write(row)
        # emit to main log if anything looks degraded
        if q_pct > 80 or write_fps < 25:
            self.log.warning(
                f"[{self._cam_name}] PERF DEGRADED at {elapsed:.0f}s: "
                f"queue={q_depth}/{self.queue_size} ({q_pct:.0f}%)  "
                f"write={write_fps:.1f}fps  overflows={self._overflows_since_last_stat}"
            )
        self._last_stat_time = now
        self._overflows_since_last_stat = 0

    def feed(self, frame):
        if self.stream is None:
            quality_param = "-cq" if "nvenc" in self.codec else "-crf"
            output_params = {"-input_framerate": self.fps, "-vcodec": self.codec, quality_param: self.crf}
            #output_params = {"-input_framerate": self.fps, "-vcodec": "h264_nvenc", "-crf": 0}
            #output_params = {"-vcodec": "libx264", "-crf": 0, "-preset": "fast"}
            self.stream = WriteGear(output=self.video_path, **output_params)
            '''
            working codecs h264_nvenc, libx264, mpeg4, mpeg2video, libxvid, libx264rgb
            
            '''
        if not self.started:
            self.start()

        if not self.Q.full():
            # add the frame to the queue
            if isinstance(frame, (list, tuple)):
                self.frame_ts.append(frame[1:])
                return self.Q.put(frame[0])
            else:
                return self.Q.put(frame)
        else:
            self._overflow_count += 1
            self._overflows_since_last_stat += 1
            self.log.warning(
                f"[{self._cam_name}] QUEUE OVERFLOW #{self._overflow_count}: "
                f"frame dropped (queue full {self.queue_size}/{self.queue_size})"
            )
            raise QueueOverflow

    # Insufficient to have consumer use while(more()) which does
    # not take into account if the producer has reached end of
    # file stream.
    def running(self):
        return self.is_active() or not self.stopped

    def is_active(self):
        # return True if there are still frames in the queue. If stream is not stopped, try to wait a moment
        tries = 0
        while self.Q.qsize() == 0 and not self.stopped and tries < 5:
            time.sleep(0.1)
            tries += 1

        return self.Q.qsize() > 0

    def wait_to_finish(self):
        while self.is_active():
            time.sleep(0.1)

    def stop(self):
        # indicate that the thread should be stopped
        self.stopped = True
        # wait until stream resources are released (producer thread might be still grabbing frame)
        if self.started:
            self.thread.join()
        if self.frame_ts:
            with open(self.video_path.replace('.mp4', '.txt'), 'w') as f:
                json.dump(self.frame_ts, f)

    def get_state(self):
        state = f'Queue {self.Q.qsize()}/{self.queue_size};'
        if self.write_speed is None:
            state += ' Write speed nan FPS'
        else:
            state += f' Write speed {(1.0 / self.write_speed):0.1f} FPS'
        return state


if __name__ == '__main__':
    NUM_FRAMES = 30*4

    # 1. TIME FAST VERSION
    import numpy as np
    writer = VideoWriterFast('./test.avi', fps=30)
    start = time.time()
    writer.start()
    for _ in range(NUM_FRAMES):
        # time.sleep(0.1)  # time it takes to produce a frame, this is why a separate thread is faster
        writer.feed(np.random.randint(0, 255, (480, 640, 3)).astype('uint8'))
    while writer.is_active():
        print('Still active')
        time.sleep(1)
    print('Not active anymore')
    print('time passed', time.time() - start)
    writer.stop()
    print('finished')
