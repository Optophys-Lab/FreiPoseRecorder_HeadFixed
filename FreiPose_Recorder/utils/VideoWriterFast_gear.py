# import the necessary packages
import logging
import multiprocessing
import time
from pathlib import Path
from queue import Empty, Full


class QueueOverflow(Exception):
   """Base class for other exceptions"""
   pass


def _writer_process(video_path, fps, codec, crf, queue, queue_size,
                    write_speed_val, queue_depth_val, overflow_val,
                    perf_log_path):
    """
    Child process entry point. Owns the WriteGear/ffmpeg pipe and the perf CSV.
    A blocking ffmpeg write here cannot stall the parent process (separate GIL).
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    from vidgear.gears import WriteGear as _WriteGear

    cam_name = _Path(video_path).stem.split('_')[-1]
    # Child-process logger — writes to the same logging config inherited where
    # possible. On Windows spawn, logging config is not inherited; falling back
    # to a basic logger is acceptable (the critical perf CSV is written directly).
    log = logging.getLogger(f'VideoWriter.{cam_name}')

    quality_param = "-cq" if "nvenc" in codec else "-crf"
    output_params = {"-input_framerate": fps, "-vcodec": codec, quality_param: crf}
    stream = _WriteGear(output=video_path, **output_params)

    perf_file = open(perf_log_path, 'w', buffering=1)
    perf_file.write('elapsed_s,frames_written,queue_depth,queue_pct,write_fps,overflows_since_last\n')

    frame_ts = []
    frames_written = 0
    last_overflow_total = 0  # snapshot of overflow_val at last stat emission
    write_speed = None  # local EMA, mirrored into write_speed_val for the parent
    start_time = None
    last_stat_time = None
    stat_interval = 10.0  # seconds between perf log rows

    def _emit_perf_stat(now):
        nonlocal last_overflow_total, last_stat_time
        elapsed = now - start_time if start_time else 0.0
        q_depth = queue_depth_val.value
        q_pct = 100.0 * q_depth / queue_size
        write_fps = (1.0 / write_speed) if write_speed else 0.0
        overflow_total = overflow_val.value
        overflows_since_last = overflow_total - last_overflow_total
        row = f"{elapsed:.1f},{frames_written},{q_depth},{q_pct:.1f},{write_fps:.2f},{overflows_since_last}\n"
        perf_file.write(row)
        if q_pct > 80 or write_fps < 25:
            log.warning(
                f"[{cam_name}] PERF DEGRADED at {elapsed:.0f}s: "
                f"queue={q_depth}/{queue_size} ({q_pct:.0f}%)  "
                f"write={write_fps:.1f}fps  overflows={overflows_since_last}"
            )
        last_stat_time = now
        last_overflow_total = overflow_total

    stopped = False
    try:
        while not stopped:
            try:
                item = queue.get(timeout=0.1)
            except Empty:
                continue

            # decrement depth as soon as we've pulled an item
            with queue_depth_val.get_lock():
                if queue_depth_val.value > 0:
                    queue_depth_val.value -= 1

            # sentinel = graceful shutdown
            if item is None:
                stopped = True
                break

            # unpack: either raw ndarray, or tuple (img, img_nr_camera, img_nr, img_ts)
            if isinstance(item, (list, tuple)):
                frame = item[0]
                frame_ts.append(tuple(item[1:]))
            else:
                frame = item

            start = _time.time()
            try:
                stream.write(frame, rgb_mode=False)
            except ValueError as e:
                print("Error writing frame to stream: {}".format(e))
                stopped = True
                break
            now = _time.time()

            if write_speed is None:
                write_speed = now - start
            else:
                write_speed = 0.85 * write_speed + 0.15 * (now - start)
            # publish to parent
            write_speed_val.value = write_speed

            frames_written += 1
            if start_time is None:
                start_time = now
                last_stat_time = now
            elif now - last_stat_time >= stat_interval:
                _emit_perf_stat(now)
    finally:
        # drain any remaining frames after sentinel (there shouldn't be any
        # past the sentinel, but if stream error caused early break we still
        # want to close cleanly)
        if start_time is not None:
            _emit_perf_stat(_time.time())  # final stat row at stop
        try:
            perf_file.close()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        if frame_ts:
            try:
                with open(video_path.replace('.mp4', '.txt'), 'w') as f:
                    _json.dump(frame_ts, f)
            except Exception as e:
                print(f"Error writing timestamp file: {e}")


class VideoWriterFast:
    """
    Utility for faster Video writing with VideoGear.
    Runs writing of frames in a separate OS process (not thread) so a blocking
    ffmpeg pipe write cannot hold the GIL and stall the camera grab loop.
    """
    def __init__(self, video_path, fps, codec="libx264", crf=0, queue_size=512):
        self.crf = crf
        self.fps = fps
        self.codec = codec
        self.video_path = video_path

        self.stopped = False
        self.started = False

        # queue depth + write speed + overflow count are shared with the child
        self._ctx = multiprocessing.get_context('spawn')
        self.queue_size = queue_size
        self.Q = self._ctx.Queue(maxsize=queue_size)
        self._queue_depth_val = self._ctx.Value('i', 0)
        self._write_speed_val = self._ctx.Value('d', 0.0)
        # cumulative overflow counter; child reads for perf CSV, parent increments
        self._overflow_val = self._ctx.Value('i', 0)

        self._cam_name = Path(video_path).stem.split('_')[-1]
        self.log = logging.getLogger(f'VideoWriter.{self._cam_name}')
        self._perf_log_path = video_path.replace('.mp4', '.perf.csv')

        self._process = None

    # -- convenience ----------------------------------------------------------
    @property
    def write_speed(self):
        """Mirrors the EMA from the child; returns None if no frames written yet."""
        v = self._write_speed_val.value
        return v if v > 0 else None

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        """starts the child process to write frames to the video file"""
        self.started = True
        self._process = self._ctx.Process(
            target=_writer_process,
            args=(self.video_path, self.fps, self.codec, self.crf,
                  self.Q, self.queue_size,
                  self._write_speed_val, self._queue_depth_val,
                  self._overflow_val,
                  self._perf_log_path),
            daemon=True,
        )
        self._process.start()
        return self

    def feed(self, frame):
        if not self.started:
            self.start()

        # Use put_nowait + Full to avoid relying on qsize() on Windows.
        try:
            self.Q.put_nowait(frame)
        except Full:
            with self._overflow_val.get_lock():
                self._overflow_val.value += 1
                total = self._overflow_val.value
            self.log.warning(
                f"[{self._cam_name}] QUEUE OVERFLOW #{total}: "
                f"frame dropped (queue full {self.queue_size}/{self.queue_size})"
            )
            raise QueueOverflow
        with self._queue_depth_val.get_lock():
            self._queue_depth_val.value += 1

    # Insufficient to have consumer use while(more()) which does
    # not take into account if the producer has reached end of
    # file stream.
    def running(self):
        return self.is_active() or not self.stopped

    def is_active(self):
        if self._process is None:
            return False
        return self._process.is_alive() or not self.Q.empty()

    def wait_to_finish(self):
        # stop() drains via sentinel; keep this as a no-op for API compatibility.
        return

    def stop(self):
        """signal the child process to drain and exit, then join."""
        if self.stopped:
            return
        self.stopped = True
        if not self.started or self._process is None:
            return

        # (a) send sentinel so the child drains queued frames and exits.
        try:
            self.Q.put(None, timeout=5.0)
        except Full:
            # queue is somehow stuck full — drain one slot by terminating anyway
            self.log.warning(f"[{self._cam_name}] could not enqueue stop sentinel; terminating")

        # (b) wait up to 120 s for the child to finish writing
        self._process.join(timeout=120.0)

        # (c) terminate if still alive
        if self._process.is_alive():
            self.log.error(
                f"[{self._cam_name}] writer process still alive after 120 s — terminating"
            )
            self._process.terminate()
            self._process.join(timeout=5.0)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=5.0)

    def get_state(self):
        q_depth = self._queue_depth_val.value
        state = f'Queue {q_depth}/{self.queue_size};'
        ws = self.write_speed
        if ws is None:
            state += ' Write speed nan FPS'
        else:
            state += f' Write speed {(1.0 / ws):0.1f} FPS'
        return state


if __name__ == '__main__':
    multiprocessing.freeze_support()
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
