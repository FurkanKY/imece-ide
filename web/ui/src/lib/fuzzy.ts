/* fuzzy.ts — bağımlılıksız subsequence fuzzy eşleme + skorlama.
   Skor: ardışık eşleşme ve kelime-başı bonusu; dosya adı eşleşmesi yol eşleşmesinden değerli. */

export interface FuzzyHit {
  score: number;
  /** eşleşen karakter indeksleri (vurgulama için) */
  indices: number[];
}

export function fuzzyMatch(query: string, target: string): FuzzyHit | null {
  if (!query) return { score: 0, indices: [] };
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  const indices: number[] = [];
  let score = 0;
  let ti = 0;
  let streak = 0;
  for (let qi = 0; qi < q.length; qi++) {
    const idx = t.indexOf(q[qi], ti);
    if (idx === -1) return null;
    // bonuslar: ardışıklık + kelime başı (önceki karakter ayraçsa)
    streak = idx === ti ? streak + 1 : 1;
    const prev = target[idx - 1];
    const wordStart = idx === 0 || prev === "/" || prev === "." || prev === "_" || prev === "-" || prev === " ";
    score += 1 + streak * 2 + (wordStart ? 6 : 0);
    indices.push(idx);
    ti = idx + 1;
  }
  // kısa hedef bonusu (daha spesifik eşleşme öne)
  score += Math.max(0, 24 - target.length / 4);
  return { score, indices };
}

export function fuzzyFilter<T>(
  query: string,
  items: T[],
  key: (item: T) => string,
  limit = 60,
): { item: T; hit: FuzzyHit }[] {
  const out: { item: T; hit: FuzzyHit }[] = [];
  for (const item of items) {
    const hit = fuzzyMatch(query, key(item));
    if (hit) out.push({ item, hit });
  }
  out.sort((a, b) => b.hit.score - a.hit.score);
  return out.slice(0, limit);
}
