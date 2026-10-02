import { memo, useEffect, useMemo, useRef } from "react";
import type { TranscriptWord } from "../api/schemas";

interface Props {
  words: TranscriptWord[];
  time: number;
  onSeek: (seconds: number) => void;
}

/** Index of the word being spoken at ``time``: the last one that has started. Binary search. */
export function activeWordIndex(words: TranscriptWord[], time: number): number {
  let low = 0;
  let high = words.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if ((words[mid]?.start ?? Infinity) <= time) {
      found = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return found;
}

/**
 * Word-by-word read-along. The highlight follows playback; clicking a word
 * jumps there. The transcript only re-renders the two words whose state
 * changed, so a long chapter stays smooth at 60 fps.
 */
export function ReadAlong({ words, time, onSeek }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const active = activeWordIndex(words, time);

  const paragraphs = useMemo(() => {
    const groups: { index: number; word: TranscriptWord }[][] = [];
    words.forEach((word, index) => {
      const last = groups[groups.length - 1];
      if (last && last[0]?.word.paragraph === word.paragraph) last.push({ index, word });
      else groups.push([{ index, word }]);
    });
    return groups;
  }, [words]);

  // Keep the spoken word in view inside the transcript box (never scroll the page).
  useEffect(() => {
    const box = container.current;
    const element = box?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    if (!box || !element) return;
    const top = element.offsetTop;
    if (top < box.scrollTop || top + element.offsetHeight > box.scrollTop + box.clientHeight) {
      box.scrollTop = Math.max(0, top - box.clientHeight / 3);
    }
  }, [active]);

  return (
    <div
      ref={container}
      className="transcript"
      aria-label="Transcript"
      onClick={(event) => {
        const index = (event.target as HTMLElement).dataset.index;
        const word = index === undefined ? undefined : words[Number(index)];
        if (word) onSeek(word.start);
      }}
    >
      {paragraphs.map((group) => (
        <p key={group[0]?.index}>
          {group.map(({ index, word }) => (
            <Word key={index} index={index} text={word.text} active={index === active} />
          ))}
        </p>
      ))}
    </div>
  );
}

const Word = memo(function Word({ index, text, active }: { index: number; text: string; active: boolean }) {
  return (
    <>
      <span
        data-index={index}
        className={active ? "word active" : "word"}
        aria-current={active ? "true" : undefined}
        title="Play from here"
      >
        {text}
      </span>{" "}
    </>
  );
});
