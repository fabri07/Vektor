import { PageWrapper } from "@/components/layout/PageWrapper";
import { IngestionPage } from "@/features/ingestion/IngestionPage";

export default function IngestionRoute() {
  return (
    <PageWrapper title="Carga de datos">
      <IngestionPage />
    </PageWrapper>
  );
}
