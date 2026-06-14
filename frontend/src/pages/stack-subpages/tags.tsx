
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";
import { X } from "lucide-react";

export function TagsConfig({ stackId }: { stackId: number }) {
  const queryClient = useQueryClient();
  const [newTag, setNewTag] = useState("");

  const { data: tags = [] } = useQuery({
    queryKey: ["stacks", stackId, "tags"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/tags` as any);
      return res.data || [];
    }
  });

  const addTag = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.POST(`/api/stacks/${stackId}/tags`, { body: { name: newTag } });
      if (res.error) throw new Error("Failed to add tag");
      return res.data as any;
    },
    onSuccess: () => {
      setNewTag("");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId, "tags"] });
      toast.success("Tag added");
    }
  });

  const removeTag = useMutation({
    mutationFn: async (tagId: number) => {
      // @ts-ignore
      const res = await api.DELETE(`/api/stacks/${stackId}/tags/${tagId}`);
      if (res.error) throw new Error("Failed to remove tag");
      return res.data as any;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId, "tags"] });
      toast.success("Tag removed");
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Tags</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2 mb-4">
          {tags.map((t: any) => (
            <Badge key={t.id} variant="secondary" className="flex items-center gap-1">
              {t.name}
              <X className="w-3 h-3 cursor-pointer" onClick={() => removeTag.mutate(t.id)} />
            </Badge>
          ))}
          {tags.length === 0 && <span className="text-sm text-muted-foreground">No tags configured</span>}
        </div>
        <div className="grid gap-2">
          <Label>Add a new tag</Label>
          <div className="flex gap-2">
            <Input placeholder="e.g. backend" value={newTag} onChange={(e) => setNewTag(e.target.value)} />
            <Button variant="secondary" onClick={() => addTag.mutate()} disabled={!newTag}>Add</Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
