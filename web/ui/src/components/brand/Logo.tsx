/* Logo — uygulamanın marka işareti: birbirine bağlı üç düğüm
   (Planner → Coder → Reviewer üçgeni). Accent'i CSS var'dan alır,
   accent değişince canlı uyum sağlar. */

export function Logo({ size = 18, glow = false }: { size?: number; glow?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      style={glow ? { filter: "drop-shadow(0 0 6px color-mix(in srgb, var(--accent) 55%, transparent))" } : undefined}
    >
      {/* bağlantılar */}
      <path
        d="M12 4.6 L5.2 18 M12 4.6 L18.8 18 M5.2 18 L18.8 18"
        stroke="color-mix(in srgb, var(--accent) 55%, transparent)"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      {/* düğümler — tepe: planner (dolu accent), alt ikili: coder/reviewer */}
      <circle cx="12" cy="4.6" r="3" fill="var(--accent)" />
      <circle cx="5.2" cy="18" r="2.6" fill="var(--card2)" stroke="var(--accent)" strokeWidth="1.5" />
      <circle cx="18.8" cy="18" r="2.6" fill="var(--card2)" stroke="var(--accent2)" strokeWidth="1.5" />
    </svg>
  );
}
