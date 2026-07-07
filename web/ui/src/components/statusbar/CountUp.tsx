/* CountUp — sayıya yumuşak sayaç animasyonu (anim.py count_to portu). */

import { useEffect, useRef, useState } from "react";

export function CountUp({ value, format }: { value: number; format: (v: number) => string }) {
  const [display, setDisplay] = useState(value);
  const fromRef = useRef(value);

  useEffect(() => {
    const from = fromRef.current;
    if (from === value) return;
    const t0 = performance.now();
    const dur = 500;
    let raf: number;
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3); // OutCubic
      setDisplay(from + (value - from) * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
      else fromRef.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value]);

  return <span>{format(display)}</span>;
}
