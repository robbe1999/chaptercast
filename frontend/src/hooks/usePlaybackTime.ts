import { useEffect, useState, type RefObject } from "react";

/**
 * The audio element's current time, updated every animation frame while
 * playing (``timeupdate`` alone fires only ~4 times a second, which makes a
 * word-by-word highlight visibly lag) and on every seek while paused.
 */
export function usePlaybackTime(audio: RefObject<HTMLAudioElement | null>): number {
  const [time, setTime] = useState(0);

  useEffect(() => {
    const element = audio.current;
    if (!element) return;
    let frame = 0;
    const sync = () => setTime(element.currentTime);
    const tick = () => {
      sync();
      frame = requestAnimationFrame(tick);
    };
    const play = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(tick);
    };
    const stop = () => {
      cancelAnimationFrame(frame);
      sync();
    };
    element.addEventListener("play", play);
    element.addEventListener("pause", stop);
    element.addEventListener("ended", stop);
    element.addEventListener("seeked", sync);
    element.addEventListener("timeupdate", sync);
    return () => {
      cancelAnimationFrame(frame);
      element.removeEventListener("play", play);
      element.removeEventListener("pause", stop);
      element.removeEventListener("ended", stop);
      element.removeEventListener("seeked", sync);
      element.removeEventListener("timeupdate", sync);
    };
  }, [audio]);

  return time;
}
