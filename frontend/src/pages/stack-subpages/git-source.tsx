import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";

export function GitSourceSettings() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Git Source Configuration</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2">
          <Label>Repository Branch</Label>
          <div className="flex gap-2">
            <Input defaultValue="main" />
            <Button variant="secondary">Save</Button>
          </div>
        </div>
        <div className="grid gap-2">
          <Label>Deploy specific commit (Optional)</Label>
          <div className="flex gap-2">
            <Input placeholder="Commit SHA" />
            <Button variant="secondary">Save</Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
