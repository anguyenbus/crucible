/*
 * Harvested from references/donna/frontend/src/lib/utils.ts
 * (MIT (c) 2026 Donna Contributors, same owner). Donna's fuzzy-match helpers
 * (tabular-review feature, out of Phase 1 scope) were dropped at harvest.
 */
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
    return twMerge(clsx(inputs));
}
