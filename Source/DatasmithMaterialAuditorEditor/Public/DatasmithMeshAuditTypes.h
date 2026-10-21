#pragma once

#include "CoreMinimal.h"

#include "DatasmithMeshAuditTypes.generated.h"

/** Categories produced by the read-only source-mesh audit. */
UENUM(BlueprintType)
enum class EDatasmithMeshAuditIssueType : uint8
{
    Unknown,

    /** 0.9.5 canonical geometry, normal, topology and actor findings. */
    InvalidNormal,
    DegenerateTriangle,
    LocalWindingConflict,
    VertexNormalReversed,
    ClosedShellInward,
    NonManifoldTopology,
    OpenMeshDirectionUnknown,
    ActorNegativeScale,

    /** Legacy normal names plus existing UV findings retained for serialized/Python and UV-tool compatibility. */
    DegenerateGeometry,
    InvalidVertexNormal,
    NonUnitVertexNormal,
    ReversedVertexNormal,
    AmbiguousVertexNormal,
    WindingConflict,
    NonManifoldEdge,
    MissingUvChannel,
    InvalidTriangleUv,
    DegenerateTriangleUv,
    UvStretch
};

UENUM(BlueprintType)
enum class EDatasmithMeshAuditSeverity : uint8
{
    Info,
    Warning,
    Error
};

/** Options for one source LOD0 audit. Distances are in Unreal units. */
USTRUCT(BlueprintType)
struct DATASMITHMATERIALAUDITOREDITOR_API FDatasmithMeshAuditOptions
{
    GENERATED_BODY()

    /** Vertex-instance UV channel to inspect. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|UV", meta = (ClampMin = "0"))
    int32 UVChannel = 0;

    /** Maximum number of triangles retained as preview samples for each issue group. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Output", meta = (ClampMin = "0", ClampMax = "512"))
    int32 MaxSampleTriangles = 64;

    /** A triangle is geometrically degenerate when the magnitude of its edge cross product is at or below this value. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Geometry", meta = (ClampMin = "0.0"))
    double GeometricAreaEpsilon = 1.0e-8;

    /** Vertex-instance normals at or below this length are invalid. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Normals", meta = (ClampMin = "0.0"))
    double NormalZeroTolerance = 1.0e-4;

    /** Allowed absolute difference between a normal's length and one. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Normals", meta = (ClampMin = "0.0"))
    double NormalUnitTolerance = 0.25;

    /** Report non-unit source normals as the NORMAL_NON_UNIT InvalidNormal detail. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Normals")
    bool bReportNonUnitNormals = true;

    /** Legacy compatibility field. 0.9.5 uses the fixed abs(dot) <= 0.2 warning rule. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Normals", meta = (ClampMin = "-1.0", ClampMax = "1.0"))
    double NormalDotWarningThreshold = 0.2;

    /** Legacy compatibility field. 0.9.5 uses the fixed dot < -0.2 error rule. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Normals", meta = (ClampMin = "-1.0", ClampMax = "1.0"))
    double NormalDotErrorThreshold = -0.2;

    /** Report the fixed abs(dot) <= 0.2 near-perpendicular warning under VertexNormalReversed detail. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Normals")
    bool bReportAmbiguousNormals = true;

    /**
     * Report CPU vertex-normal versus triangle-winding direction candidates.
     *
     * This Source LOD0 vertex-instance check only condemns clearly reversed
     * normals (dot < -0.2) and reports near-perpendicular normals as warnings;
     * it never requires a smooth normal to match the face normal exactly.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|Normals")
    bool bReportNormalDirectionMismatch = true;

    /** A UV triangle is degenerate when the absolute 2D determinant is at or below this value. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|UV", meta = (ClampMin = "0.0"))
    double UVAreaEpsilon = 1.0e-10;

    /** UV-to-world anisotropy at or above this ratio is a warning. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|UV", meta = (ClampMin = "1.0"))
    double UVStretchWarningThreshold = 3.0;

    /** UV-to-world anisotropy at or above this ratio is an error. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|UV", meta = (ClampMin = "1.0"))
    double UVStretchErrorThreshold = 6.0;

    /** Absolute component scale of the actor/component using this mesh. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Datasmith Mesh Audit|UV")
    FVector WorldScale = FVector::OneVector;
};

/** One aggregated issue. SampleVertices stores three consecutive local-space vertices per TriangleIds entry. */
USTRUCT(BlueprintType)
struct DATASMITHMATERIALAUDITOREDITOR_API FDatasmithMeshAuditIssue
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    EDatasmithMeshAuditIssueType IssueType = EDatasmithMeshAuditIssueType::Unknown;

    /** Stable language-neutral identifier suitable for Python/report serialization. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FName IssueCode = NAME_None;

    /** Stable detail within IssueCode, for example NORMAL_NON_UNIT or NORMAL_NEAR_PERPENDICULAR. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FName DetailCode = NAME_None;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    EDatasmithMeshAuditSeverity Severity = EDatasmithMeshAuditSeverity::Info;

    /** Static Mesh material slot, or -1 when the issue crosses slots or has no applicable slot. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 MaterialSlot = -1;

    /** Number of offending triangles, vertex instances, or edges represented by this group. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 Count = 0;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    double MaxMetric = 0.0;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FString Description;

    /** Always zero for the current Source LOD0 audit. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 LODIndex = 0;

    /** Stable identifier for one connected finding region. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FString RegionId;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 RegionIndex = 0;

    /** Complete Source MeshDescription LOD0 triangle set for this region. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    TArray<int32> ErrorTriangleIds;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FString TriangleIdSpace = TEXT("source_mesh_description_lod0");

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FVector LocalBoundsMin = FVector::ZeroVector;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FVector LocalBoundsMax = FVector::ZeroVector;

    /** True when ErrorTriangleIds contains more faces than the preview sample arrays. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    bool bSamplesTruncated = false;

    /** Source MeshDescription LOD0 triangle IDs retained for viewport preview only. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    TArray<int32> TriangleIds;

    /** Flattened local-space vertices: [T0V0,T0V1,T0V2,T1V0,...]. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    TArray<FVector> SampleVertices;

    /** One local-space face normal for each sampled triangle. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    TArray<FVector> SampleFaceNormals;
};

USTRUCT(BlueprintType)
struct DATASMITHMATERIALAUDITOREDITOR_API FDatasmithMeshAuditResult
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    bool Success = false;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FString Error;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FString MeshPath;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 TriangleCount = 0;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 UVChannelCount = 0;

    /** Source LOD0 provenance, with a suffix when build settings recompute render normals. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    FString NormalDataSource;

    /** Closed orientable manifold shells eligible for whole-shell direction analysis. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 ClosedShellDirectionCandidateCount = 0;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 ClosedShellDirectionEvaluatedCount = 0;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    int32 ClosedShellDirectionAbstainedCount = 0;

    /** Explicit reasons why whole-shell direction was not evaluated. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    TArray<FString> DirectionAbstentionReasons;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    TArray<FDatasmithMeshAuditIssue> Issues;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit")
    double AnalysisMilliseconds = 0.0;
};

/** Component-instance transform audit, intentionally separate from source-mesh findings. */
USTRUCT(BlueprintType)
struct DATASMITHMATERIALAUDITOREDITOR_API FDatasmithComponentTransformAuditResult
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit|Transform")
    bool Success = false;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit|Transform")
    FString Error;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit|Transform")
    double Determinant = 0.0;

    /** True when the complete component-to-world matrix reverses orientation. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Datasmith Mesh Audit|Transform")
    bool bOrientationReversing = false;
};
