import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, waitFor } from "@testing-library/react";

import { AttachmentPicker } from "../components/AttachmentPicker";
import { filesService } from "@/services/files.service";

jest.mock("@/services/files.service", () => ({
  filesService: {
    upload: jest.fn(),
  },
}));

const mockUpload = filesService.upload as jest.MockedFunction<typeof filesService.upload>;

describe("AttachmentPicker", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUpload.mockResolvedValue({
      id: "file-1",
      original_filename: "demo",
      content_type: "text/plain",
      size_bytes: 100,
      purpose: "chat",
      status: "uploaded",
    });
  });

  test("accepts txt docx and pptx files for chat uploads", async () => {
    const onAdd = jest.fn();
    const onUpdate = jest.fn();

    const { container } = render(
      <AttachmentPicker
        attachments={[]}
        onAdd={onAdd}
        onUpdate={onUpdate}
        onRemove={jest.fn()}
        onRetry={jest.fn()}
      />,
    );

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.accept).toContain(".txt");
    expect(input.accept).toContain(".docx");
    expect(input.accept).toContain(".pptx");

    const txtFile = new File(["hola"], "notas.txt", { type: "text/plain" });
    const docxFile = new File(["doc"], "resumen.docx", { type: "application/octet-stream" });
    const pptxFile = new File(["ppt"], "slides.pptx", { type: "application/octet-stream" });

    fireEvent.change(input, {
      target: { files: [txtFile, docxFile, pptxFile] },
    });

    await waitFor(() => {
      expect(mockUpload).toHaveBeenCalledTimes(3);
    });
    expect(mockUpload).toHaveBeenNthCalledWith(1, txtFile, "chat");
    expect(mockUpload).toHaveBeenNthCalledWith(2, docxFile, "chat");
    expect(mockUpload).toHaveBeenNthCalledWith(3, pptxFile, "chat");
    expect(onAdd).toHaveBeenCalledTimes(3);
    expect(onUpdate).toHaveBeenCalledTimes(3);
  });
});
