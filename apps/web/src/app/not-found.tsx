import Link from "next/link";
import { Wordmark } from "@/components/marketing";

export default function NotFound() {
  return (
    <main id="main" className="mx-auto flex min-h-dvh max-w-md flex-col items-center justify-center gap-4 p-6 text-center">
      <Wordmark className="text-3xl" />
      <h1 className="text-2xl">We can&apos;t find that page</h1>
      <p className="text-muted">The link may be out of date, or the page may have moved.</p>
      <div className="flex gap-4 text-sm font-semibold">
        <Link href="/" className="text-accent hover:underline">Home</Link>
        <Link href="/dashboard" className="text-accent hover:underline">Your comics</Link>
      </div>
    </main>
  );
}
