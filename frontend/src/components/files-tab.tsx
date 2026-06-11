import { EmptyState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
/**
 * Week 16 UI: Files tab — embedded Filebrowser (iframe over the panel's
 * authenticated /files proxy) + open-full-screen.
 */
import { useMutation } from "@tanstack/react-query";
import { ExternalLink, FolderOpen } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

type Site = components["schemas"]["SiteResponse"];

async function fetchSessionUrl(siteId: number): Promise<string> {
  const { data, error } = await api.POST("/api/sites/{site_id}/files-session", {
    params: { path: { site_id: siteId } },
  });
  if (error || !data) throw new Error(apiErrorMessage(error, "Could not open the file manager"));
  return data.url;
}

export function FilesTab({ site }: { site: Site }) {
  const [embedUrl, setEmbedUrl] = useState<string | null>(null);

  const open = useMutation({
    mutationFn: () => fetchSessionUrl(site.id),
    onSuccess: setEmbedUrl,
    onError: (err) => toast.error(err.message),
  });

  const openFullScreen = useMutation({
    mutationFn: () => fetchSessionUrl(site.id),
    onSuccess: (url) => {
      window.open(url, "_blank", "noopener");
    },
    onError: (err) => toast.error(err.message),
  });

  if (site.status !== "active") {
    return <p className="text-sm text-muted-foreground">Available once the site is active.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {!embedUrl && (
          <Button onClick={() => open.mutate()} loading={open.isPending}>
            <FolderOpen className="h-4 w-4" aria-hidden /> Open file manager
          </Button>
        )}
        <Button
          variant="outline"
          onClick={() => openFullScreen.mutate()}
          loading={openFullScreen.isPending}
        >
          <ExternalLink className="h-4 w-4" aria-hidden /> Open full screen
        </Button>
      </div>

      {embedUrl ? (
        <iframe
          src={embedUrl}
          title={`Files for ${site.domain}`}
          className="h-[70vh] w-full rounded-lg border border-border bg-background"
        />
      ) : (
        <EmptyState
          title="File manager"
          description={`Browse and edit the files of ${site.domain} — scoped to this site only.`}
        />
      )}
    </div>
  );
}
