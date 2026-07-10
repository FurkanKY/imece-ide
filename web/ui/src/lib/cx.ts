/* cx — minimal className birleştirici (clsx yerine; bağımlılık eklemeden).
   Falsy değerleri atar, boşlukla birleştirir. */

export type ClassValue = string | number | false | null | undefined;

export function cx(...parts: ClassValue[]): string {
  return parts.filter(Boolean).join(" ");
}
