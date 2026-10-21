#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"

#include "DatasmithMeshAuditTypes.h"
#include "DatasmithMeshAuditLibrary.generated.h"

class UStaticMesh;
class UStaticMeshComponent;
class USceneComponent;

/** Read-only Source LOD0 inspection entry point used by the Python UI. */
UCLASS()
class DATASMITHMATERIALAUDITOREDITOR_API UDatasmithMeshAuditLibrary final : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintCallable, Category = "Datasmith Mesh Audit")
    static FDatasmithMeshAuditResult AnalyzeStaticMesh(UStaticMesh* StaticMesh, FDatasmithMeshAuditOptions Options);

    /**
     * Draws red-style wireframe triangle outlines over mesh-audit samples.
     * LocalTriangleVertices must contain exactly three component-local positions per face.
     * Returns the number of valid source faces submitted to the line batcher.
     */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Mesh Audit|Preview")
    static int32 DrawTriangleFaceHighlights(
        USceneComponent* SceneComponent,
        const TArray<FVector>& LocalTriangleVertices,
        FLinearColor Color,
        float LifeTime = -1.0f,
        bool bForeground = true,
        bool bClearExisting = true);

    /** Clears only the mesh-audit wireframe highlights in this component's world. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Mesh Audit|Preview")
    static void ClearTriangleFaceHighlights(USceneComponent* SceneComponent);

    /**
     * Reconstructs Source MeshDescription LOD0 regions by triangle ID and draws
     * red wireframe triangle outlines. MaxFaces is hard-clamped to 100,000;
     * the return value lets the caller report any safety truncation.
     */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Mesh Audit|Preview")
    static int32 DrawStaticMeshTriangleHighlights(
        UStaticMeshComponent* StaticMeshComponent,
        const TArray<int32>& SourceTriangleIds,
        FLinearColor Color,
        float LifeTime = -1.0f,
        bool bForeground = true,
        bool bClearExisting = true,
        int32 MaxFaces = 100000);

    /** Audits the complete inherited component-to-world transform determinant. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Mesh Audit|Transform")
    static FDatasmithComponentTransformAuditResult AnalyzeSceneComponentTransform(
        USceneComponent* SceneComponent);
};
