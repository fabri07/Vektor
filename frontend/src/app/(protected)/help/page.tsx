import { PageWrapper } from "@/components/layout/PageWrapper";
import { HelpChat } from "@/features/help/HelpChat";

export const metadata = { title: "Ayuda — Véktor" };

export default function HelpPage() {
  return (
    <PageWrapper title="Ayuda">
      <div className="mx-auto h-[calc(100vh-8rem)] max-w-2xl">
        <HelpChat />
      </div>
    </PageWrapper>
  );
}
