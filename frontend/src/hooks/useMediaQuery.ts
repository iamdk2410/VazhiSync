import { useSyncExternalStore } from "react";

/** Live result of a CSS media query (SSR/test safe: false without matchMedia). */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false),
    () => false,
  );
}

/** Phone-sized screens get the mobile layouts from the designs (390 px artboards). */
export const MOBILE_QUERY = "(max-width: 720px)";
export const useIsMobile = () => useMediaQuery(MOBILE_QUERY);

export const usePrefersReducedMotion = () => useMediaQuery("(prefers-reduced-motion: reduce)");
