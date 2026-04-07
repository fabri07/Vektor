import { api } from "@/lib/api";
import type { UploadedFileResponse } from "@/types/api";

export const filesService = {
  async upload(file: File, purpose = "general"): Promise<UploadedFileResponse> {
    const form = new FormData();
    form.append("file", file);
    form.append("purpose", purpose);
    const res = await api.post<UploadedFileResponse>("/files/upload", form, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    return res.data;
  },
};
