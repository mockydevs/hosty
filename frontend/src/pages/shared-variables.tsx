import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Plus, Trash2, Edit } from "lucide-react";
import { toast } from "sonner";
import { ErrorState, LoadingState } from "@/components/states";

export function SharedVariablesPage() {
  const queryClient = useQueryClient();
  const [isOpen, setIsOpen] = useState(false);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [description, setDescription] = useState("");

  const { data: variables, isLoading, error } = useQuery({
    queryKey: ["shared-variables"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/shared-variables/");
      if (error) throw new Error("Failed to fetch shared variables");
      return data;
    },
  });

  const createVar = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/shared-variables/", {
        body: { key, value, description: description || undefined },
      });
      if (error) throw new Error("Failed to create shared variable");
      return data;
    },
    onSuccess: () => {
      toast.success("Shared variable created");
      setIsOpen(false);
      setKey("");
      setValue("");
      setDescription("");
      queryClient.invalidateQueries({ queryKey: ["shared-variables"] });
    },
    onError: (error) => {
      toast.error(error.message);
    },
  });

  const deleteVar = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await api.DELETE("/api/shared-variables/{var_id}", {
        params: { path: { var_id: id } },
      });
      if (error) throw new Error("Failed to delete variable");
    },
    onSuccess: () => {
      toast.success("Variable deleted");
      queryClient.invalidateQueries({ queryKey: ["shared-variables"] });
    },
  });

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState error={error as Error} />;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Shared Variables</h1>
          <p className="text-sm text-muted-foreground">Manage global environment variables across all projects.</p>
        </div>
        <Dialog open={isOpen} onOpenChange={setIsOpen}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="mr-2 h-4 w-4" /> Add Variable
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Add Shared Variable</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 pt-4">
              <div className="space-y-2">
                <Label>Key</Label>
                <Input placeholder="DATABASE_URL" value={key} onChange={(e) => setKey(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label>Value</Label>
                <Input placeholder="postgres://..." value={value} onChange={(e) => setValue(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label>Description (optional)</Label>
                <Input placeholder="Primary database connection string" value={description} onChange={(e) => setDescription(e.target.value)} />
              </div>
              <Button className="w-full" onClick={() => createVar.mutate()} disabled={createVar.isPending}>
                Save Variable
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Key</TableHead>
              <TableHead>Value</TableHead>
              <TableHead>Description</TableHead>
              <TableHead className="w-[100px]"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {!variables || variables.length === 0 ? (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-muted-foreground h-24">
                  No shared variables configured.
                </TableCell>
              </TableRow>
            ) : (
              variables.map((v) => (
                <TableRow key={v.id}>
                  <TableCell className="font-medium">{v.key}</TableCell>
                  <TableCell className="font-mono text-sm max-w-[200px] truncate">{v.value}</TableCell>
                  <TableCell className="text-muted-foreground">{v.description || "-"}</TableCell>
                  <TableCell className="text-right">
                    <Button variant="ghost" size="icon" onClick={() => deleteVar.mutate(v.id)}>
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
