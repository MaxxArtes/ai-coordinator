import React from "react";
import { band, INDICATOR_LABEL, normalizeReading, readingValueText, type IndicatorReading } from "./readings";

const R = 5.5;
const CIRCUMFERENCE = 2 * Math.PI * R;

/**
 * A 14 px ring with a visible label ("ctx", "5h", "7d") and its value. Decorative for assistive technology: the
 * button around the rings carries the full accessible name. No animation in any state.
 * - unknown: dashed track and "—", never a "%";
 * - measured: a full arc and "18%"; estimated: a dashed arc and "≈18%"; a fixture also says "fixture".
 */
export const IndicatorRing: React.FC<{ reading: IndicatorReading }> = ({ reading: input }) => {
  const reading = normalizeReading(input);
  const known = reading.provenance !== "unknown" && reading.percent !== undefined;
  const percent = reading.percent ?? 0;
  const arc = known ? (CIRCUMFERENCE * percent) / 100 : 0;
  const angle = (percent / 100) * 2 * Math.PI;
  const x = 7 + R * Math.sin(angle);
  const y = 7 - R * Math.cos(angle);
  const estimatePath = percent === 100
    ? `M 7 1.5 A ${R} ${R} 0 1 1 7 12.5 A ${R} ${R} 0 1 1 7 1.5`
    : `M 7 1.5 A ${R} ${R} 0 ${percent > 50 ? 1 : 0} 1 ${x} ${y}`;
  const classes = [
    "indicator-ring",
    `indicator-ring--${reading.provenance}`,
    known ? `indicator-ring--${band(reading.percent as number)}` : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={classes} aria-hidden="true">
      <svg className="indicator-ring__svg" viewBox="0 0 14 14" focusable="false">
        <circle className="indicator-ring__track" cx="7" cy="7" r={R} fill="none" strokeWidth={2} />
        {known && reading.provenance === "estimated" && percent > 0 && (
          <path className="indicator-ring__arc" d={estimatePath} fill="none" strokeWidth={2} strokeDasharray="2 1.5" />
        )}
        {known && reading.provenance === "measured" && (
          <circle
            className="indicator-ring__arc"
            cx="7"
            cy="7"
            r={R}
            fill="none"
            strokeWidth={2}
            strokeDasharray={`${arc} ${CIRCUMFERENCE}`}
            transform="rotate(-90 7 7)"
          />
        )}
      </svg>
      <span className="indicator-ring__label">{INDICATOR_LABEL[reading.kind].short}</span>
      <span className="indicator-ring__value">{readingValueText(reading)}</span>
      {reading.origin === "test_fixture" && <span className="indicator-ring__fixture">fixture</span>}
    </span>
  );
};
