import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";

export function TagsPage() {
  const queryClient = useQueryClient();

  const { data: tags, isLoading, error } = useQuery({
    queryKey: ["global-tags"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/tags/");
      if (error) throw new Error("Failed to fetch tags");
      return data as any; /* as any */
    },
  });

  const deleteTag = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await api.DELETE("/api/tags/{tag_id}", {
        params: { path: { tag_id: id } },
      });
      if (error) throw new Error("Failed to delete tag");
    },
    onSuccess: () => {
      toast.success("Tag deleted globally");
      queryClient.invalidateQueries({ queryKey: ["global-tags"] });
    },
    onError: (error) => {
      toast.error(error.message);
    },
  });

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState message={error instanceof Error ? error.message : String(error)} />;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Global Tags</h1>
        <p className="text-sm text-muted-foreground">Manage organizational tags used across all projects.</p>
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Tag Name</TableHead>
              <TableHead>Projects Using Tag</TableHead>
              <TableHead className="w-[100px]"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {!tags || tags.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3} className="text-center text-muted-foreground h-24">
                  No tags created yet. Add tags from a project's settings page.
                </TableCell>
              </TableRow>
            ) : (
              tags.map((t: any) => (
                <TableRow key={t.id}>
                  <TableCell>
                    <Badge variant="secondary">{t.name}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {t.usage_count} project{t.usage_count === 1 ? "" : "s"}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button variant="ghost" size="icon" onClick={() => deleteTag.mutate(t.id)}>
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
