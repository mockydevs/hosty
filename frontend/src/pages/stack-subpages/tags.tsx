import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export function TagsConfig() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Tags</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex gap-2 mb-4">
          <Badge variant="secondary">production</Badge>
          <Badge variant="secondary">frontend</Badge>
        </div>
        <div className="grid gap-2">
          <Label>Add a new tag</Label>
          <div className="flex gap-2">
            <Input placeholder="e.g. backend" />
            <Button variant="secondary">Add</Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
