"use client";

import { useParams } from "next/navigation";
import { TenantDetailScreen } from "@/features/admin/TenantDetailScreen";

export default function AdminTenantDetailPage() {
  const params = useParams<{ id: string }>();
  return <TenantDetailScreen tenantId={params.id} />;
}
