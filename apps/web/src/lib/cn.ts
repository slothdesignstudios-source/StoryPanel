import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Class names merged with Tailwind conflict resolution. Usable from server and client components. */
export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));
